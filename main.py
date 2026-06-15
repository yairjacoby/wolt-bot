"""
Wolt Restaurant Availability Bot for Telegram
===============================================
Built by Yair (with Claude) — Project #1

This bot checks if restaurants on Wolt are currently available for delivery.
You can:
  1. Search for restaurants near you
  2. Check if a specific restaurant is online
  3. Set a monitor to get pinged when a restaurant opens

How it works:
  - Wolt has a public API (consumer-api.wolt.com) that their app/website uses
  - This bot calls that same API to check restaurant status
  - When you set a monitor, it checks every 2 minutes and alerts you when status changes
"""

import os
import json
import asyncio
import logging
import requests
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ──────────────────────────────────────────────
# CONFIGURATION
# ──────────────────────────────────────────────

# You'll set these as environment variables when deploying (instructions below)
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "YOUR_TOKEN_HERE")

# Tel Aviv coordinates (default location)
DEFAULT_LAT = 32.0853
DEFAULT_LON = 34.7818

# How often to check monitored restaurants (in seconds)
MONITOR_INTERVAL = 120  # 2 minutes

# File to persist monitoring state across restarts
MONITOR_FILE = "monitored.json"

# ──────────────────────────────────────────────
# WOLT API LAYER
# This is where the magic happens — we talk to Wolt's servers
# ──────────────────────────────────────────────

WOLT_API_BASE = "https://consumer-api.wolt.com"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)",
    "Accept": "application/json",
    "platform": "Web",
}


def _extract_venue(item: dict) -> dict | None:
    """Extract venue data from a Wolt API item, handling different data structures."""
    # Wolt's API nests venue data differently depending on the section/version
    venue = item.get("venue", {}) or {}

    if not venue and "image" in item:
        venue = item.get("image", {}) or {}

    if not venue and "template" in item:
        venue = item["template"].get("venue", {}) or {}

    # Some items nest venue inside "track_id" or other structures
    if not venue:
        for key in ["venue_data", "data"]:
            if key in item and isinstance(item[key], dict):
                venue = item[key]
                break

    if not venue or not venue.get("name"):
        return None

    return {
        "name": venue.get("name", ""),
        "slug": venue.get("slug", ""),
        "online": venue.get("online", False),
        "delivers": venue.get("delivers", False),
        "short_description": venue.get("short_description", ""),
        "address": venue.get("address", venue.get("street_address", "")),
        "rating": venue.get("rating", {}).get("score", None) if isinstance(venue.get("rating"), dict) else venue.get("rating"),
        "estimate_minutes": venue.get("estimate", 0),
        "delivery_price": venue.get("delivery_price_int", 0),
    }


def _matches_query(query: str, venue: dict) -> bool:
    """
    Flexible matching — checks if ANY word in the query appears in the 
    restaurant name, slug, description, or address.

    Examples that will work:
      "vitrina" → matches "Vitrina Ibn Gabirol", "Vitrina Rothschild", etc.
      "pizza" → matches "Pizza Hut", "Domino's Pizza", etc.
      "sushi jaffa" → matches if name has "sushi" OR restaurant is in "jaffa"
    """
    query_words = query.lower().split()
    searchable = " ".join([
        venue.get("name", ""),
        venue.get("slug", "").replace("-", " "),
        venue.get("short_description", ""),
        venue.get("address", ""),
    ]).lower()

    # Match if ALL query words appear somewhere in the searchable text
    return all(word in searchable for word in query_words)


def search_restaurants(query: str, lat: float = DEFAULT_LAT, lon: float = DEFAULT_LON) -> list:
    """
    Search for restaurants on Wolt near a location.
    Returns a list of restaurant dicts with name, slug, status, etc.

    Supports partial names: "vitrina" finds all Vitrina branches.
    Supports multi-word: "sushi ramat" finds sushi places in Ramat area.
    """
    url = f"{WOLT_API_BASE}/v1/pages/restaurants"
    params = {"lat": lat, "lon": lon}

    try:
        response = requests.get(url, params=params, headers=HEADERS, timeout=10)
        response.raise_for_status()
        data = response.json()

        # Collect ALL venues from ALL sections (not just "All restaurants")
        restaurants = []
        seen_slugs = set()  # Avoid duplicates across sections

        for section in data.get("sections", []):
            for item in section.get("items", []):
                venue = _extract_venue(item)
                if not venue:
                    continue

                # Skip duplicates (same restaurant can appear in multiple sections)
                if venue["slug"] in seen_slugs:
                    continue

                # Check if it matches the search query
                if _matches_query(query, venue):
                    seen_slugs.add(venue["slug"])
                    restaurants.append(venue)

        return restaurants

    except requests.RequestException as e:
        logging.error(f"Wolt API error: {e}")
        return []


def check_venue_status(slug: str) -> dict | None:
    """
    Check a specific restaurant's current (live) status by its slug.
    Uses the same restaurants endpoint as search — the /static endpoint
    does not include real-time online status.
    """
    url = f"{WOLT_API_BASE}/v1/pages/restaurants"
    params = {"lat": DEFAULT_LAT, "lon": DEFAULT_LON}

    try:
        response = requests.get(url, params=params, headers=HEADERS, timeout=10)
        response.raise_for_status()
        data = response.json()

        for section in data.get("sections", []):
            for item in section.get("items", []):
                venue = _extract_venue(item)
                if venue and venue["slug"] == slug:
                    return venue

        return None

    except requests.RequestException as e:
        logging.error(f"Wolt venue check error for {slug}: {e}")
        return None


# ──────────────────────────────────────────────
# MONITORING ENGINE
# Stores which restaurants each user is watching
# ──────────────────────────────────────────────

# Persisted store: { chat_id: { slug: { name, last_status, ... } } }
# Saved to MONITOR_FILE so state survives process restarts.

def _load_monitored() -> dict:
    try:
        with open(MONITOR_FILE) as f:
            data = json.load(f)
        return {int(k): v for k, v in data.items()}  # JSON keys are always strings
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_monitored() -> None:
    with open(MONITOR_FILE, "w") as f:
        json.dump(monitored, f)


monitored = _load_monitored()


async def monitor_loop(app: Application) -> None:
    """
    Background task that checks all monitored restaurants every MONITOR_INTERVAL seconds.
    If a restaurant changes from offline → online, sends an alert.
    """
    while True:
        await asyncio.sleep(MONITOR_INTERVAL)

        for chat_id, restaurants in list(monitored.items()):
            for slug, info in list(restaurants.items()):
                status = await asyncio.to_thread(check_venue_status, slug)
                if status is None:
                    continue

                was_online = info.get("last_status", False)
                is_online = status["online"]

                # Alert if restaurant just came online!
                if is_online and not was_online:
                    await app.bot.send_message(
                        chat_id=chat_id,
                        text=(
                            f"🟢 *{status['name']}* is now OPEN on Wolt!\n\n"
                            f"🕐 Estimated delivery: ~{status['estimate_minutes']} min\n"
                            f"📱 Order now: https://wolt.com/en/isr/tel-aviv/restaurant/{slug}\n\n"
                            f"Use /unmonitor {slug} to stop alerts."
                        ),
                        parse_mode="Markdown",
                    )

                # Also alert if it went offline (optional, useful to know)
                elif not is_online and was_online:
                    await app.bot.send_message(
                        chat_id=chat_id,
                        text=f"🔴 *{status['name']}* just went offline on Wolt.",
                        parse_mode="Markdown",
                    )

                # Update stored status
                info["last_status"] = is_online
                _save_monitored()


# ──────────────────────────────────────────────
# TELEGRAM COMMAND HANDLERS
# These are the commands users can send to the bot
# ──────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start command — welcome message."""
    await update.message.reply_text(
        "🍕 *Wolt Availability Bot*\n\n"
        "I check if restaurants on Wolt are open for delivery.\n\n"
        "*Commands:*\n"
        "🔍 /search `name` — Find restaurants (partial names work!)\n"
        "📊 /check `restaurant-slug` — Check if a specific restaurant is online\n"
        "👁 /monitor `restaurant-slug` — Get alerts when it opens\n"
        "🛑 /unmonitor `restaurant-slug` — Stop alerts\n"
        "📋 /watching — See your monitored restaurants\n\n"
        "*Examples:*\n"
        "`/search vitrina` — finds all Vitrina branches\n"
        "`/search pizza` — finds all pizza places\n"
        "`/search sushi jaffa` — finds sushi places in Jaffa\n\n"
        "💡 _After searching, tap buttons to check or monitor._",
        parse_mode="Markdown",
    )


async def search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /search command — search for restaurants on Wolt."""
    if not context.args:
        await update.message.reply_text("Usage: `/search restaurant name`", parse_mode="Markdown")
        return

    query = " ".join(context.args)
    await update.message.reply_text(f"🔍 Searching Wolt for *{query}*...", parse_mode="Markdown")

    results = search_restaurants(query)

    if not results:
        await update.message.reply_text(
            f"No restaurants found matching '{query}'.\n\n"
            "💡 *Tips:*\n"
            "• Try shorter names: `/search vitrina` instead of the full name\n"
            "• Try food type: `/search pizza` or `/search sushi`\n"
            "• Try area: `/search jaffa`",
            parse_mode="Markdown",
        )
        return

    count = len(results)
    shown = min(count, 8)
    header = f"Found *{count}* result{'s' if count > 1 else ''}:"
    if count > 8:
        header += f" (showing top {shown})"
    await update.message.reply_text(header, parse_mode="Markdown")

    for r in results[:8]:  # Show top 8 matches (more results for branches)
        status_emoji = "🟢" if r["online"] else "🔴"
        status_text = "OPEN" if r["online"] else "CLOSED"

        # Create inline buttons for quick actions
        keyboard = [
            [
                InlineKeyboardButton("📊 Check Now", callback_data=f"check:{r['slug']}"),
                InlineKeyboardButton("👁 Monitor", callback_data=f"monitor:{r['slug']}:{r['name']}"),
            ],
            [
                InlineKeyboardButton(
                    "📱 Open in Wolt",
                    url=f"https://wolt.com/en/isr/tel-aviv/restaurant/{r['slug']}",
                ),
            ],
        ]

        rating_text = f"⭐ {r['rating']}/5" if r.get('rating') else ""
        delivery_text = f"🚗 ~{r['estimate_minutes']} min" if r.get('estimate_minutes') else ""
        address_text = f"📍 {r['address']}" if r.get('address') else ""

        await update.message.reply_text(
            f"{status_emoji} *{r['name']}* — {status_text}\n"
            f"{address_text}\n"
            f"{r.get('short_description', '')}\n"
            f"{rating_text}  {delivery_text}",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )


async def check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /check command — check a specific restaurant's status."""
    if not context.args:
        await update.message.reply_text("Usage: `/check restaurant-slug`", parse_mode="Markdown")
        return

    slug = context.args[0]
    await update.message.reply_text(f"Checking *{slug}*...", parse_mode="Markdown")

    status = check_venue_status(slug)

    if status is None:
        await update.message.reply_text(
            f"Couldn't find restaurant with slug '{slug}'.\n"
            "Use /search to find the correct slug."
        )
        return

    emoji = "🟢" if status["online"] else "🔴"
    text = "OPEN for orders!" if status["online"] else "Currently CLOSED"

    await update.message.reply_text(
        f"{emoji} *{status['name']}* — {text}\n\n"
        f"🕐 Est. delivery: ~{status['estimate_minutes']} min\n"
        f"📱 https://wolt.com/en/isr/tel-aviv/restaurant/{slug}",
        parse_mode="Markdown",
    )


async def monitor_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /monitor command — start watching a restaurant."""
    if not context.args:
        await update.message.reply_text("Usage: `/monitor restaurant-slug`", parse_mode="Markdown")
        return

    slug = context.args[0]
    chat_id = update.effective_chat.id

    # Check if restaurant exists first
    status = check_venue_status(slug)
    if status is None:
        await update.message.reply_text(
            f"Couldn't find '{slug}' on Wolt. Use /search to find the correct slug."
        )
        return

    # Add to monitoring
    if chat_id not in monitored:
        monitored[chat_id] = {}

    monitored[chat_id][slug] = {
        "name": status["name"],
        "last_status": status["online"],
        "started": datetime.now().isoformat(),
    }
    _save_monitored()

    current = "🟢 OPEN" if status["online"] else "🔴 CLOSED"

    await update.message.reply_text(
        f"👁 Now monitoring *{status['name']}*\n\n"
        f"Current status: {current}\n"
        f"I'll ping you when the status changes.\n"
        f"Checking every {MONITOR_INTERVAL // 60} minutes.\n\n"
        f"Use `/unmonitor {slug}` to stop.",
        parse_mode="Markdown",
    )


async def unmonitor_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /unmonitor command — stop watching a restaurant."""
    if not context.args:
        await update.message.reply_text("Usage: `/unmonitor restaurant-slug`", parse_mode="Markdown")
        return

    slug = context.args[0]
    chat_id = update.effective_chat.id

    if chat_id in monitored and slug in monitored[chat_id]:
        name = monitored[chat_id][slug]["name"]
        del monitored[chat_id][slug]
        _save_monitored()
        await update.message.reply_text(f"🛑 Stopped monitoring *{name}*.", parse_mode="Markdown")
    else:
        await update.message.reply_text("You're not monitoring that restaurant.")


async def watching_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /watching command — show all monitored restaurants."""
    chat_id = update.effective_chat.id
    user_monitors = monitored.get(chat_id, {})

    if not user_monitors:
        await update.message.reply_text(
            "You're not monitoring any restaurants.\nUse `/search` to find one, then `/monitor` it.",
            parse_mode="Markdown",
        )
        return

    lines = ["👁 *Your Monitored Restaurants:*\n"]
    for slug, info in user_monitors.items():
        status = "🟢" if info.get("last_status") else "🔴"
        lines.append(f"{status} {info['name']} (`{slug}`)")

    lines.append(f"\n_Checking every {MONITOR_INTERVAL // 60} minutes._")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle inline button presses (Check Now / Monitor buttons from search results)."""
    query = update.callback_query
    await query.answer()

    data = query.data
    if data.startswith("check:"):
        slug = data.split(":", 1)[1]
        status = check_venue_status(slug)
        if status:
            emoji = "🟢" if status["online"] else "🔴"
            text = "OPEN" if status["online"] else "CLOSED"
            await query.message.reply_text(
                f"{emoji} *{status['name']}* is currently *{text}*",
                parse_mode="Markdown",
            )
        else:
            await query.message.reply_text("Couldn't check this restaurant right now.")

    elif data.startswith("monitor:"):
        parts = data.split(":", 2)
        slug = parts[1]
        name = parts[2] if len(parts) > 2 else slug
        chat_id = query.message.chat_id

        status = check_venue_status(slug)
        if chat_id not in monitored:
            monitored[chat_id] = {}

        monitored[chat_id][slug] = {
            "name": name,
            "last_status": status["online"] if status else False,
            "started": datetime.now().isoformat(),
        }
        _save_monitored()

        await query.message.reply_text(
            f"👁 Now monitoring *{name}*. I'll ping you when it opens/closes.",
            parse_mode="Markdown",
        )


async def post_init(app: Application) -> None:
    """Start the background monitoring loop after bot initializes."""
    asyncio.create_task(monitor_loop(app))


# ──────────────────────────────────────────────
# RUN THE BOT
# ──────────────────────────────────────────────

def main():
    """Start the bot."""
    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        level=logging.INFO,
    )

    if TELEGRAM_TOKEN == "YOUR_TOKEN_HERE":
        print("\n❌ ERROR: No Telegram token set!")
        print("Set it as an environment variable:")
        print("  export TELEGRAM_TOKEN='your-token-from-botfather'")
        print("\nSee DEPLOY.md for full instructions.\n")
        return

    app = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()

    # Register commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("search", search))
    app.add_handler(CommandHandler("check", check))
    app.add_handler(CommandHandler("monitor", monitor_cmd))
    app.add_handler(CommandHandler("unmonitor", unmonitor_cmd))
    app.add_handler(CommandHandler("watching", watching_cmd))
    app.add_handler(CallbackQueryHandler(handle_button))

    print("🍕 Wolt Bot is running! Press Ctrl+C to stop.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
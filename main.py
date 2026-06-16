"""
Wolt Restaurant Availability Bot for Telegram
===============================================
Built by Yair (with Claude) — Project #1

Checks restaurant availability on Wolt and sends alerts when they open.
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

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "YOUR_TOKEN_HERE")
DEFAULT_LAT = 32.0853
DEFAULT_LON = 34.7818
MONITOR_INTERVAL = 120  # seconds between checks
MONITOR_FILE = "monitored.json"

# ──────────────────────────────────────────────
# WOLT API LAYER
# ──────────────────────────────────────────────

WOLT_API_BASE = "https://consumer-api.wolt.com"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)",
    "Accept": "application/json",
    "platform": "Web",
}


def _extract_venue(item: dict) -> dict | None:
    """Extract venue data from a Wolt API item, handling different data structures."""
    venue = item.get("venue", {}) or {}
    if not venue and "image" in item:
        venue = item.get("image", {}) or {}
    if not venue and "template" in item:
        venue = item["template"].get("venue", {}) or {}
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
    query_words = query.lower().split()
    searchable = " ".join([
        venue.get("name", ""),
        venue.get("slug", "").replace("-", " "),
        venue.get("short_description", ""),
        venue.get("address", ""),
    ]).lower()
    return all(word in searchable for word in query_words)


def search_restaurants(query: str, lat: float = DEFAULT_LAT, lon: float = DEFAULT_LON) -> list:
    """Search for restaurants on Wolt near a location. Supports partial names and multi-word."""
    url = f"{WOLT_API_BASE}/v1/pages/restaurants"
    params = {"lat": lat, "lon": lon}
    try:
        response = requests.get(url, params=params, headers=HEADERS, timeout=10)
        response.raise_for_status()
        data = response.json()
        restaurants = []
        seen_slugs = set()
        for section in data.get("sections", []):
            for item in section.get("items", []):
                venue = _extract_venue(item)
                if not venue or venue["slug"] in seen_slugs:
                    continue
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
    """Background task: checks all monitored restaurants and fires alerts on status change."""
    while True:
        await asyncio.sleep(MONITOR_INTERVAL)
        for chat_id, restaurants in list(monitored.items()):
            for slug, info in list(restaurants.items()):
                status = await asyncio.to_thread(check_venue_status, slug)
                if status is None:
                    continue
                was_online = info.get("last_status", False)
                is_online = status["online"]
                if is_online and not was_online:
                    eta = f"~{status['estimate_minutes']} min" if status.get("estimate_minutes") else "soon"
                    await app.bot.send_message(
                        chat_id=chat_id,
                        text=(
                            f"🚨 *{status['name']}* is OPEN right now! Go go go! 🏃\n\n"
                            f"⏱ Estimated delivery: {eta}\n"
                            f"👉 [Order now on Wolt](https://wolt.com/en/isr/tel-aviv/restaurant/{slug})"
                        ),
                        parse_mode="Markdown",
                    )
                elif not is_online and was_online:
                    await app.bot.send_message(
                        chat_id=chat_id,
                        text=(
                            f"😴 *{status['name']}* just closed on Wolt.\n"
                            f"I'll keep watching and ping you when it's back!"
                        ),
                        parse_mode="Markdown",
                    )
                info["last_status"] = is_online
                _save_monitored()


# ──────────────────────────────────────────────
# UX HELPERS
# ──────────────────────────────────────────────

def _is_hebrew(text: str) -> bool:
    return any('֐' <= c <= '׿' for c in text)


def _main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🔍 Search a restaurant", callback_data="menu:search"),
        InlineKeyboardButton("👁 My watch list", callback_data="menu:watching"),
    ]])


# Keywords that signal the user wants to stop monitoring something
_UNMONITOR_TRIGGERS = [
    "stop monitoring", "stop watching", "unmonitor", "remove", "delete",
    "don't want", "dont want", "cancel monitoring", "cancel watching",
    # Hebrew
    "תסיר", "תפסיק", "הסר", "מחק", "בטל", "הפסק",
]


def _is_unmonitor_request(text: str) -> bool:
    t = text.lower()
    return any(kw in t for kw in _UNMONITOR_TRIGGERS)


def _find_unmonitor_targets(text: str, user_monitors: dict) -> list[str]:
    """Return slugs of monitored restaurants whose name appears in the free-text message."""
    text_lower = text.lower()
    matched = []
    for slug, info in user_monitors.items():
        # Match on any significant word (>2 chars) from the restaurant name
        name_words = [w for w in info["name"].lower().split() if len(w) > 2]
        if any(w in text_lower for w in name_words):
            matched.append(slug)
    return matched


async def _do_monitor(chat_id: int, slug: str, name: str, current_online: bool) -> None:
    """Add a restaurant to monitoring."""
    if chat_id not in monitored:
        monitored[chat_id] = {}
    monitored[chat_id][slug] = {
        "name": name,
        "last_status": current_online,
        "started": datetime.now().isoformat(),
    }
    _save_monitored()


async def _do_unmonitor(chat_id: int, slug: str) -> str | None:
    """Remove a restaurant from monitoring. Returns its name if it was being watched."""
    if chat_id in monitored and slug in monitored[chat_id]:
        name = monitored[chat_id][slug]["name"]
        del monitored[chat_id][slug]
        _save_monitored()
        return name
    return None


async def _run_search(message, query: str, hebrew: bool = False) -> None:
    """Run a Wolt search and reply with results + inline buttons."""
    if hebrew:
        await message.reply_text(f"מחפש ב-Wolt: *{query}* ⏳", parse_mode="Markdown")
    else:
        await message.reply_text(f"Searching Wolt for *{query}*... ⏳", parse_mode="Markdown")

    results = await asyncio.to_thread(search_restaurants, query)

    if not results:
        if hebrew:
            await message.reply_text(
                f"לא מצאתי מסעדות שמתאימות ל-'{query}' 😕\n\n"
                "נסה שם קצר יותר, סוג אוכל (פיצה, סושי), או אזור (יפו, פלורנטין)",
            )
        else:
            await message.reply_text(
                f"Hmm, nothing found for *'{query}'* 😕\n\n"
                "Try a shorter name, food type (pizza, sushi), or area (jaffa, florentin).",
                parse_mode="Markdown",
            )
        return

    count = len(results)
    if hebrew:
        header = f"מצאתי *{count}* תוצאות! 🎉" + (" (מציג 8 ראשונות)" if count > 8 else "")
    else:
        header = f"Found *{count}* match{'es' if count > 1 else ''}! 🎉" + (" (showing top 8)" if count > 8 else "")
    await message.reply_text(header, parse_mode="Markdown")

    for r in results[:8]:
        status_emoji = "🟢" if r["online"] else "🔴"
        status_text = ("פתוח" if r["online"] else "סגור") if hebrew else ("OPEN" if r["online"] else "CLOSED")
        rating_text = f"⭐ {r['rating']}/5" if r.get("rating") else ""
        delivery_text = f"🚗 ~{r['estimate_minutes']} min" if r.get("estimate_minutes") else ""
        address_text = f"📍 {r['address']}" if r.get("address") else ""

        keyboard = [
            [
                InlineKeyboardButton("📊 Check status", callback_data=f"check:{r['slug']}"),
                InlineKeyboardButton("👁 Watch it", callback_data=f"monitor:{r['slug']}:{r['name']}"),
            ],
            [
                InlineKeyboardButton(
                    "📱 Open in Wolt",
                    url=f"https://wolt.com/en/isr/tel-aviv/restaurant/{r['slug']}",
                ),
            ],
        ]

        await message.reply_text(
            f"{status_emoji} *{r['name']}* — {status_text}\n"
            f"{address_text}\n"
            f"{r.get('short_description', '')}\n"
            f"{rating_text}  {delivery_text}".strip(),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )


async def _send_status_card(message, status: dict) -> None:
    """Send a rich status card for one restaurant with Watch / Open buttons."""
    slug = status["slug"]
    if status["online"]:
        emoji, text = "🟢", "Open and taking orders!"
    else:
        emoji, text = "🔴", "Closed right now"
    eta = f"~{status['estimate_minutes']} min" if status.get("estimate_minutes") else "—"
    keyboard = [[
        InlineKeyboardButton("👁 Watch it", callback_data=f"monitor:{slug}:{status['name']}"),
        InlineKeyboardButton("📱 Open in Wolt", url=f"https://wolt.com/en/isr/tel-aviv/restaurant/{slug}"),
    ]]
    await message.reply_text(
        f"{emoji} *{status['name']}* — {text}\n\n"
        f"⏱ Estimated delivery: {eta}",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def _show_watch_list(message, chat_id: int) -> None:
    """Send the user's watch list, each entry with a Remove button."""
    user_monitors = monitored.get(chat_id, {})
    if not user_monitors:
        await message.reply_text(
            "You're not watching any restaurants yet 👀\n\n"
            "Want to add one? Just type a restaurant name and I'll search!",
            reply_markup=_main_menu_keyboard(),
        )
        return

    count = len(user_monitors)
    await message.reply_text(
        f"👁 *You're watching {count} restaurant{'s' if count > 1 else ''}:*\n"
        f"_Checking every {MONITOR_INTERVAL // 60} min — I'll ping you when anything changes._",
        parse_mode="Markdown",
    )

    for slug, info in user_monitors.items():
        status_emoji = "🟢" if info.get("last_status") else "🔴"
        keyboard = [[
            InlineKeyboardButton("🗑 Remove", callback_data=f"unmonitor:{slug}"),
            InlineKeyboardButton("📱 Open in Wolt", url=f"https://wolt.com/en/isr/tel-aviv/restaurant/{slug}"),
        ]]
        await message.reply_text(
            f"{status_emoji} *{info['name']}*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )


# ──────────────────────────────────────────────
# TELEGRAM HANDLERS
# ──────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Hey! 👋 I'm your Wolt watch-bot 🍕\n\n"
        "I keep an eye on restaurants and ping you the moment they open for delivery.\n\n"
        "What do you want to do?",
        reply_markup=_main_menu_keyboard(),
    )


async def search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text(
            "Just type the restaurant name after /search — like `/search vitrina` or `/search pizza` 🍕",
            parse_mode="Markdown",
        )
        return
    query = " ".join(context.args)
    await _run_search(update.message, query)


async def check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text(
            "Give me the restaurant slug: `/check restaurant-slug`\n"
            "Not sure of the slug? Use 🔍 Search to find it.",
            parse_mode="Markdown",
        )
        return
    slug = context.args[0]
    await update.message.reply_text(f"Give me a sec, checking *{slug}*... ⏳", parse_mode="Markdown")
    status = await asyncio.to_thread(check_venue_status, slug)
    if status is None:
        await update.message.reply_text(
            f"Hmm, I couldn't find *{slug}* on Wolt 🤔\n"
            "Double-check the slug, or use 🔍 Search to find the right one.",
            parse_mode="Markdown",
        )
        return
    await _send_status_card(update.message, status)


async def monitor_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text(
            "Tell me which restaurant to watch: `/monitor restaurant-slug`",
            parse_mode="Markdown",
        )
        return
    slug = context.args[0]
    chat_id = update.effective_chat.id
    status = await asyncio.to_thread(check_venue_status, slug)
    if status is None:
        await update.message.reply_text(
            f"Couldn't find *{slug}* on Wolt 🤔 Use 🔍 Search to find the correct slug.",
            parse_mode="Markdown",
        )
        return
    await _do_monitor(chat_id, slug, status["name"], status["online"])
    current = "open right now 🟢" if status["online"] else "closed right now 🔴"
    await update.message.reply_text(
        f"Got it! 👀 I'm now watching *{status['name']}* for you.\n\n"
        f"It's {current} — I'll ping you the moment the status changes!\n"
        f"Checking every {MONITOR_INTERVAL // 60} min.",
        parse_mode="Markdown",
    )


async def unmonitor_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text(
            "Tell me which restaurant to stop watching: `/unmonitor restaurant-slug`",
            parse_mode="Markdown",
        )
        return
    slug = context.args[0]
    chat_id = update.effective_chat.id
    name = await _do_unmonitor(chat_id, slug)
    if name:
        await update.message.reply_text(f"Done! 👍 I'll stop watching *{name}*.", parse_mode="Markdown")
    else:
        await update.message.reply_text("I wasn't watching that one 🤷")


async def watching_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _show_watch_list(update.message, update.effective_chat.id)


async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle all inline button presses."""
    query = update.callback_query
    await query.answer()
    data = query.data
    chat_id = query.message.chat_id

    if data == "menu:search":
        await query.message.reply_text(
            "Sure! Just type the restaurant name or food type and I'll search Wolt 🔍\n\n"
            "_Example: vitrina, sushi, pizza jaffa_",
            parse_mode="Markdown",
        )

    elif data == "menu:watching":
        await _show_watch_list(query.message, chat_id)

    elif data.startswith("check:"):
        slug = data.split(":", 1)[1]
        status = await asyncio.to_thread(check_venue_status, slug)
        if status:
            await _send_status_card(query.message, status)
        else:
            await query.message.reply_text("Couldn't reach Wolt right now — try again in a sec 🙏")

    elif data.startswith("monitor:"):
        parts = data.split(":", 2)
        slug = parts[1]
        name = parts[2] if len(parts) > 2 else slug
        status = await asyncio.to_thread(check_venue_status, slug)
        await _do_monitor(chat_id, slug, name, status["online"] if status else False)
        current = "open right now 🟢" if (status and status["online"]) else "closed right now 🔴"
        await query.message.reply_text(
            f"Got it! 🍕 I'm now watching *{name}* for you.\n\n"
            f"It's {current} — I'll ping you the moment it opens!",
            parse_mode="Markdown",
        )

    elif data.startswith("unmonitor:"):
        slug = data.split(":", 1)[1]
        name = await _do_unmonitor(chat_id, slug)
        if name:
            await query.message.reply_text(
                f"Done! ✅ Stopped watching *{name}*.",
                parse_mode="Markdown",
            )
            await _show_watch_list(query.message, chat_id)
        else:
            await query.message.reply_text("Looks like I wasn't watching that one 🤷")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handle free-text messages.
    - If the message looks like an unmonitor request → find matching restaurant and remove it.
    - Otherwise → treat as a search query.
    """
    text = update.message.text
    chat_id = update.effective_chat.id
    user_monitors = monitored.get(chat_id, {})
    hebrew = _is_hebrew(text)

    if _is_unmonitor_request(text) and user_monitors:
        targets = _find_unmonitor_targets(text, user_monitors)
        if targets:
            removed = []
            for slug in targets:
                name = await _do_unmonitor(chat_id, slug)
                if name:
                    removed.append(name)
            if removed:
                names_str = ", ".join(f"*{n}*" for n in removed)
                if hebrew:
                    await update.message.reply_text(
                        f"סגור! ✅ הפסקתי לעקוב אחרי {names_str}.",
                        parse_mode="Markdown",
                    )
                else:
                    await update.message.reply_text(
                        f"Done! ✅ Stopped watching {names_str}.",
                        parse_mode="Markdown",
                    )
                return

        # Unmonitor keyword detected but couldn't identify which restaurant
        if hebrew:
            await update.message.reply_text(
                "איזו מסעדה? 🤔 תפתח את רשימת המעקב ומשם תוכל להסיר בלחיצה.",
                reply_markup=_main_menu_keyboard(),
            )
        else:
            await update.message.reply_text(
                "Which restaurant? 🤔 Open your watch list and tap Remove.",
                reply_markup=_main_menu_keyboard(),
            )
        return

    # Anything else → search
    await _run_search(update.message, text, hebrew=hebrew)


async def post_init(app: Application) -> None:
    asyncio.create_task(monitor_loop(app))


# ──────────────────────────────────────────────
# RUN THE BOT
# ──────────────────────────────────────────────

def main():
    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        level=logging.INFO,
    )

    if TELEGRAM_TOKEN == "YOUR_TOKEN_HERE":
        print("\n❌ ERROR: No Telegram token set!")
        print("Set it as an environment variable:")
        print("  export TELEGRAM_TOKEN='your-token-from-botfather'")
        return

    app = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("search", search))
    app.add_handler(CommandHandler("check", check))
    app.add_handler(CommandHandler("monitor", monitor_cmd))
    app.add_handler(CommandHandler("unmonitor", unmonitor_cmd))
    app.add_handler(CommandHandler("watching", watching_cmd))
    app.add_handler(CallbackQueryHandler(handle_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("🍕 Wolt Bot is running! Press Ctrl+C to stop.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

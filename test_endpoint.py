"""
Run this to confirm check_venue_status returns real online status.
Usage: python test_endpoint.py <slug>
Example: python test_endpoint.py vitrina-rothschild
"""
import sys
from main import check_venue_status, search_restaurants

if len(sys.argv) > 1:
    slug = sys.argv[1]
    print(f"\nChecking slug: {slug}")
    result = check_venue_status(slug)
    if result:
        print(f"  name:    {result['name']}")
        print(f"  online:  {result['online']}")   # <-- this is the field that must be True/False correctly
        print(f"  delivers:{result['delivers']}")
        print(f"  eta:     {result['estimate_minutes']} min")
    else:
        print("  Not found in the restaurants list (restaurant may be outside Tel Aviv radius)")
else:
    print("\nNo slug given — searching for 'vitrina' to find a real slug to test with:")
    results = search_restaurants("vitrina")
    for r in results[:5]:
        print(f"  slug={r['slug']}  online={r['online']}  name={r['name']}")
    print("\nRe-run with: python test_endpoint.py <slug>")

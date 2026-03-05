#!/usr/bin/env python3
"""
polymarket_btc_price.py
=======================
Extract the BTC strike-price field (e.g. $72,535.16) from a Polymarket
BTC Up/Down 5-minute market event.

Target page:
    https://polymarket.com/zh/event/btc-updown-5m-1772695800

The strike price is set by the Chainlink BTC/USD oracle at the start of
each 5-minute window and is embedded in:
  - event["question"]  / market["question"]   — as human-readable text
  - event["strikePrice"] / market["strikePrice"] — as a numeric value

Two extraction strategies are provided and run in order:
  1. Polymarket Gamma API  (fast, no browser required)
  2. __NEXT_DATA__ HTML    (parse Next.js SSR JSON block from raw HTML)
  3. Playwright browser    (fallback, renders the JS page and parses the DOM)

Usage
-----
    # Install dependencies (once):
    pip install requests playwright
    playwright install chromium

    # Run:
    python scripts/polymarket_btc_price.py
    python scripts/polymarket_btc_price.py --slug btc-updown-5m-1772695800
    python scripts/polymarket_btc_price.py --url https://polymarket.com/event/btc-updown-5m-1772695800
"""

import argparse
import json
import re
import sys
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_SLUG = "btc-updown-5m-1772695800"
DEFAULT_URL = f"https://polymarket.com/zh/event/{DEFAULT_SLUG}"
GAMMA_API_BASE = "https://gamma-api.polymarket.com"

# Regex that matches dollar amounts such as $72,535.16 or $72535
PRICE_RE = re.compile(r"\$[\d,]+(?:\.\d+)?")


# ---------------------------------------------------------------------------
# Helper: extract a formatted dollar price from a string
# ---------------------------------------------------------------------------

def _find_price(text: str) -> list[str]:
    """Return all dollar-price matches found in *text*."""
    return PRICE_RE.findall(text or "")


# ---------------------------------------------------------------------------
# Strategy 1: Polymarket Gamma API
# ---------------------------------------------------------------------------

def fetch_via_api(slug: str) -> dict | None:
    """
    Query the Gamma API for the event identified by *slug*.

    Returns a dict with keys:
        "source"      – "gamma_api"
        "field"       – the JSON field path where the price was found
        "raw_value"   – the raw value of that field
        "price"       – the formatted dollar price string, e.g. "$72,535.16"
        "event"       – the full event JSON (for inspection)

    Returns None if the event cannot be reached or no price is found.
    """
    try:
        import requests  # noqa: PLC0415
    except ImportError:
        print("[API] 'requests' not installed – skipping API strategy.", file=sys.stderr)
        return None

    # Polymarket Gamma API supports two slug formats:
    #   /events?slug=<slug>          – returns a list
    #   /events/slug/<slug>          – returns a single object (may 404 for some events)
    urls_to_try = [
        f"{GAMMA_API_BASE}/events?slug={slug}",
        f"{GAMMA_API_BASE}/events/slug/{slug}",
    ]

    event = None
    for api_url in urls_to_try:
        try:
            resp = requests.get(api_url, timeout=20)
            if resp.status_code == 200:
                data = resp.json()
                # /events?slug=… returns a list
                if isinstance(data, list):
                    if data:
                        event = data[0]
                        break
                elif isinstance(data, dict) and data:
                    event = data
                    break
        except Exception as exc:  # noqa: BLE001
            print(f"[API] Request to {api_url} failed: {exc}", file=sys.stderr)

    if event is None:
        print("[API] Could not retrieve event from Gamma API.", file=sys.stderr)
        return None

    # -------------------------------------------------------------------
    # Walk through fields that are known to carry the strike price
    # -------------------------------------------------------------------
    candidate_paths = [
        # (field_path_for_display, value)
        ("event.strikePrice",   str(event.get("strikePrice", ""))),
        ("event.question",      event.get("question", "")),
        ("event.title",         event.get("title", "")),
        ("event.description",   event.get("description", "")),
    ]

    for market in event.get("markets", []):
        candidate_paths += [
            (f"market[{market.get('id','?')}].strikePrice",   str(market.get("strikePrice", ""))),
            (f"market[{market.get('id','?')}].question",      market.get("question", "")),
            (f"market[{market.get('id','?')}].title",         market.get("title", "")),
        ]

    for field_path, raw_value in candidate_paths:
        prices = _find_price(raw_value)
        if prices:
            return {
                "source":    "gamma_api",
                "field":     field_path,
                "raw_value": raw_value,
                "price":     prices[0],
                "event":     event,
            }

    # If no regex match, try returning a numeric strikePrice directly
    sp = event.get("strikePrice")
    if sp is not None:
        return {
            "source":    "gamma_api",
            "field":     "event.strikePrice",
            "raw_value": str(sp),
            "price":     f"${float(sp):,.2f}",
            "event":     event,
        }

    print("[API] Event found but no price field located.", file=sys.stderr)
    return None


# ---------------------------------------------------------------------------
# Strategy 2: __NEXT_DATA__ JSON embedded in HTML (Next.js SSR pages)
# ---------------------------------------------------------------------------

# Polymarket is built on Next.js.  The server-side-rendered HTML includes a
# <script id="__NEXT_DATA__" type="application/json"> tag that contains the
# full page props, including market/event data, without needing JavaScript
# execution.  This is the lightest fallback before launching a real browser.

NEXT_DATA_RE = re.compile(
    r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
    re.DOTALL,
)


def _walk_json(obj: Any, path: str = "") -> list[tuple[str, Any]]:
    """Recursively yield (path, value) for every leaf in a JSON structure."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _walk_json(v, f"{path}.{k}" if path else k)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _walk_json(v, f"{path}[{i}]")
    else:
        yield path, obj


def fetch_via_next_data(page_url: str) -> dict | None:
    """
    Fetch the raw HTML of *page_url* and extract the price from the
    ``__NEXT_DATA__`` JSON block embedded by Next.js SSR.

    Returns the same dict shape as :func:`fetch_via_api`, or None on failure.
    """
    try:
        import requests  # noqa: PLC0415
    except ImportError:
        print("[NextData] 'requests' not installed.", file=sys.stderr)
        return None

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }

    try:
        resp = requests.get(page_url, headers=headers, timeout=30)
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        print(f"[NextData] HTTP request failed: {exc}", file=sys.stderr)
        return None

    match = NEXT_DATA_RE.search(resp.text)
    if not match:
        print("[NextData] No __NEXT_DATA__ block found in page HTML.", file=sys.stderr)
        return None

    try:
        next_data = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        print(f"[NextData] Failed to parse __NEXT_DATA__ JSON: {exc}", file=sys.stderr)
        return None

    # Walk every leaf value in the JSON tree and look for dollar prices
    for field_path, value in _walk_json(next_data):
        prices = _find_price(str(value))
        if prices:
            return {
                "source":    "__NEXT_DATA__",
                "field":     field_path,
                "raw_value": str(value),
                "price":     prices[0],
                "event":     next_data,
            }

    # If no formatted price was found, look for a numeric strikePrice field
    for field_path, value in _walk_json(next_data):
        if "strike" in field_path.lower() and isinstance(value, (int, float)):
            return {
                "source":    "__NEXT_DATA__",
                "field":     field_path,
                "raw_value": str(value),
                "price":     f"${float(value):,.2f}",
                "event":     next_data,
            }

    print("[NextData] __NEXT_DATA__ found but no price field located.", file=sys.stderr)
    return None


# ---------------------------------------------------------------------------
# Strategy 3: Playwright browser (JS-rendered page scraping)
# ---------------------------------------------------------------------------

def fetch_via_browser(page_url: str) -> dict | None:
    """
    Open *page_url* in a headless Chromium browser, wait for the React app
    to render, then scan visible text for the dollar price.

    Returns the same dict shape as :func:`fetch_via_api`, or None on failure.
    """
    try:
        from playwright.sync_api import sync_playwright  # noqa: PLC0415
    except ImportError:
        print("[Browser] 'playwright' not installed – skipping browser strategy.", file=sys.stderr)
        return None

    print(f"[Browser] Launching headless Chromium for {page_url} …")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(
            locale="zh-CN",
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        page = ctx.new_page()

        try:
            page.goto(page_url, wait_until="networkidle", timeout=60_000)
        except Exception as exc:  # noqa: BLE001
            print(f"[Browser] Navigation failed: {exc}", file=sys.stderr)
            browser.close()
            return None

        # Collect all text nodes visible in the DOM
        text_nodes: list[str] = page.evaluate(
            """() => {
                const walker = document.createTreeWalker(
                    document.body,
                    NodeFilter.SHOW_TEXT,
                    null,
                    false
                );
                const texts = [];
                let node;
                while ((node = walker.nextNode())) {
                    const t = node.textContent.trim();
                    if (t) texts.push(t);
                }
                return texts;
            }"""
        )

        # Also grab page title and the full inner-text for broader coverage
        title = page.title()
        inner_text = page.inner_text("body")

        browser.close()

    # Search for price pattern
    all_text = " ".join(text_nodes) + " " + title + " " + inner_text
    prices = _find_price(all_text)

    if prices:
        return {
            "source":    "browser",
            "field":     "DOM text node",
            "raw_value": all_text[:200] + "…",
            "price":     prices[0],
            "event":     None,
        }

    print("[Browser] Page rendered but no dollar-price pattern found.", file=sys.stderr)
    return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract the BTC strike-price field from a Polymarket event page."
    )
    parser.add_argument(
        "--slug",
        default=DEFAULT_SLUG,
        help=f"Polymarket event slug (default: {DEFAULT_SLUG})",
    )
    parser.add_argument(
        "--url",
        default=DEFAULT_URL,
        help=f"Full page URL to scrape as fallback (default: {DEFAULT_URL})",
    )
    parser.add_argument(
        "--json",
        dest="output_json",
        action="store_true",
        help="Output the full result as JSON",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    print("=" * 60)
    print("Polymarket BTC Strike-Price Extractor")
    print("=" * 60)
    print(f"Event slug : {args.slug}")
    print(f"Page URL   : {args.url}")
    print()

    # --- Strategy 1: Gamma API ---
    print("[ Strategy 1 ] Polymarket Gamma API …")
    result = fetch_via_api(args.slug)

    # --- Strategy 2: __NEXT_DATA__ from raw HTML (Next.js SSR) ---
    if result is None:
        print()
        print("[ Strategy 2 ] __NEXT_DATA__ JSON from page HTML …")
        result = fetch_via_next_data(args.url)

    # --- Strategy 3: Playwright browser (fallback) ---
    if result is None:
        print()
        print("[ Strategy 3 ] Playwright browser scraping …")
        result = fetch_via_browser(args.url)

    # --- Report ---
    print()
    if result is None:
        print("❌  Could not extract the BTC strike price.")
        print("    Make sure 'requests' and/or 'playwright' are installed.")
        return 1

    print("✅  BTC strike price found!")
    print(f"    Source     : {result['source']}")
    print(f"    Field path : {result['field']}")
    print(f"    Raw value  : {result['raw_value']}")
    print(f"    Price      : {result['price']}")

    if args.output_json:
        output = {k: v for k, v in result.items() if k != "event"}
        if result.get("event"):
            output["event"] = result["event"]
        print()
        print(json.dumps(output, indent=2, ensure_ascii=False))

    return 0


if __name__ == "__main__":
    sys.exit(main())

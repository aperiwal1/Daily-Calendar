#!/usr/bin/env python3
"""
Daily Economic Calendar + Earnings Bot
Posts US & Canada economic events and earnings to Slack

Economic Calendar: Claude API with web search
Earnings: Direct Nasdaq API (deterministic, no AI)
"""

import os
import sys
import json
import logging
import re
import time
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path

import anthropic
import requests
from dotenv import load_dotenv

load_dotenv()

# ============= CONFIGURATION =============
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL")
CACHE_FILE = Path("last_calendar.json")
REQUEST_TIMEOUT = 30
MAX_CONTENT_RETRIES = 2

# Nasdaq API
NASDAQ_EARNINGS_URL = "https://api.nasdaq.com/api/calendar/earnings"
NASDAQ_HEADERS = {
    "accept": "application/json, text/plain, */*",
    "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "origin": "https://www.nasdaq.com",
    "referer": "https://www.nasdaq.com/market-activity/earnings",
}

# Priority watchlist tickers to bold in output
WATCHLIST_US = ["TSLA", "NVDA", "AMZN", "AAPL", "META", "MSFT", "PLTR", "GOOG", "GOOGL",
                "AMD", "IREN", "SOFI", "NFLX", "MSTR", "BRK.B", "RKLB", "AVGO", "TSM",
                "MU", "HOOD", "NBIS", "ASTS"]
WATCHLIST_CAD = ["ENB", "SHOP", "TD", "RY", "T", "BNS", "BCE", "IAG", "CNQ", "CM",
                 "POW", "BMO", "DOL", "CLS", "PSLV", "WCP", "CSU", "SU", "SCZ", "BN", "CNR"]
WATCHLIST = set(WATCHLIST_US + WATCHLIST_CAD)

# Earnings config
MIN_MARKET_CAP = 1_000_000_000  # $1B
MAX_PER_SECTION = 6

# ============= LOGGING =============
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('calendar_bot.log')
    ]
)
logger = logging.getLogger(__name__)

# ============= ECONOMIC CALENDAR PROMPT (no earnings) =============
ECON_PROMPT_TEMPLATE = """
CRITICAL CONTEXT:
- Today is {today_date} ({today_weekday})
- You are searching for: {tomorrow_date_short} ({tomorrow_weekday})
- If today is Friday, you are looking for MONDAY's calendar - search accordingly

Search for {tomorrow_date}'s US and Canada ECONOMIC CALENDAR ONLY. Do NOT include earnings.

SEARCH STRATEGY (do all searches):
1. Search: "US economic calendar {tomorrow_date_search}"
2. Search: "Canada economic calendar {tomorrow_date_search}" OR "StatCan releases {tomorrow_date_search}"

═══════════════════════════════════════════════════
KNOWN RECURRING ECONOMIC EVENTS - USE AS A CHECKLIST
═══════════════════════════════════════════════════
Cross-reference your search results against these. If the target date falls on the matching weekday, VERIFY whether these are scheduled:

WEEKLY (almost every week):
• Thursday 08:30 ET: 🇺🇸 Initial Jobless Claims — THIS IS WEEKLY, EVERY THURSDAY. If {tomorrow_weekday} is Thursday, this MUST appear unless it's a holiday.
• Wednesday 10:30 ET: 🇺🇸 EIA Crude Oil Inventories
• Monday 15:00 ET: 🇺🇸 Treasury International Capital (TIC) data (monthly but released on a Monday)

MONTHLY (check if date matches):
• First Friday: 🇺🇸 Nonfarm Payrolls & Unemployment Rate (08:30 ET)
• First business day: 🇺🇸 ISM Manufacturing PMI (10:00 ET)
• Third business day: 🇺🇸 ISM Services PMI (10:00 ET)
• ~10th-15th: 🇺🇸 CPI (08:30 ET), 🇺🇸 PPI (08:30 ET)
• ~15th: 🇺🇸 Retail Sales (08:30 ET)
• ~20th: 🇨🇦 CPI (08:30 ET)
• ~25th-28th: 🇺🇸 GDP (08:30 ET), 🇺🇸 PCE Price Index (08:30 ET), 🇺🇸 Durable Goods (08:30 ET)
• Mid-month: 🇺🇸 Industrial Production (09:15 ET), 🇺🇸 Housing Starts & Building Permits (08:30 ET)
• Various: 🇺🇸 Philadelphia Fed Manufacturing Index (Thursday, 08:30 ET, third week)
• Various: 🇺🇸 Michigan Consumer Sentiment (Friday, 10:00 ET, mid/end month)
• Various: 🇨🇦 GDP (end of month), 🇨🇦 Employment (first Friday after US NFP)
• 8 times/year: 🇺🇸 FOMC Rate Decision (14:00 ET), 🇨🇦 BoC Rate Decision (10:00 ET)

This checklist is a HINT, not a guarantee. Always verify with your search results. But if it's Thursday and Jobless Claims doesn't appear in your output, something is WRONG — search again.
═══════════════════════════════════════════════════

OUTPUT THIS EXACT FORMAT (economic data ONLY, no earnings section):

*Economic Data:*
• [Time] ET: 🇺🇸 [US Event]
• [Time] ET: 🇨🇦 [Canada Event]

STRICT RULES:
1. EVERY economic event gets its own bullet point - never combine multiple events on one line
2. EVERY economic event MUST have a country flag: 🇺🇸 for US, 🇨🇦 for Canada - NO EXCEPTIONS
3. SORT ECONOMIC DATA BY TIME - earliest first (08:30 before 09:45 before 10:00) - THIS IS MANDATORY
4. Output ONLY the economic data section - no preamble, notes, explanations, sources, no earnings
5. Search for Canada data (StatCan, BoC) - if none scheduled, don't include any
6. If genuinely no economic data after multiple searches: • No major releases scheduled
7. Use abbreviations: CPI, PPI, GDP, PCE, PMI, BoC, FOMC
8. Sort economic events by time STRICTLY ASCENDING (e.g., 08:30, 08:30, 09:45, 10:00, 11:45)
9. Start with *Economic Data:* - no text before it
10. Do NOT include any earnings information - that is handled separately

EXAMPLE OUTPUT:
*Economic Data:*
• 08:15 ET: 🇺🇸 ADP Employment Change (Feb)
• 08:30 ET: 🇺🇸 Initial Jobless Claims (week ending Feb 22)
• 10:00 ET: 🇺🇸 ISM Services PMI (Feb)
• 10:00 ET: 🇺🇸 Factory Orders (Jan)
• 10:30 ET: 🇺🇸 EIA Crude Oil Inventories"""


# ============= UTILITIES =============
def retry_with_backoff(max_retries=3, base_delay=2, exceptions=(Exception,)):
    """Decorator for retrying functions with exponential backoff."""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    if attempt == max_retries - 1:
                        logger.error(f"All {max_retries} attempts failed for {func.__name__}")
                        raise
                    delay = base_delay * (2 ** attempt)
                    logger.warning(f"Attempt {attempt + 1} failed: {e}. Retrying in {delay}s...")
                    time.sleep(delay)
        return wrapper
    return decorator


def get_next_trading_day() -> datetime:
    """Return the next trading day, skipping weekends."""
    today = datetime.now()
    days_ahead = 1
    if today.weekday() == 4:
        days_ahead = 3
    elif today.weekday() == 5:
        days_ahead = 2
    elif today.weekday() == 6:
        days_ahead = 1
    next_day = today + timedelta(days=days_ahead)
    logger.info(f"Today is {today.strftime('%A')} - next trading day: {next_day.strftime('%A, %b %d')}")
    return next_day


def validate_economic_data(text: str) -> tuple[bool, str | None]:
    """Validate that the economic data response has expected structure."""
    if not text:
        return False, "Empty response"
    if len(text) < 20:
        return False, f"Response too short ({len(text)} chars)"
    if "*Economic Data:*" not in text and "Economic Data:" not in text:
        return False, "Missing Economic Data section"
    unwanted = ["Important Note", "Note:", "disclaimer", "not available", "shutdown", "beyond current"]
    for phrase in unwanted:
        if phrase.lower() in text.lower():
            return False, f"Contains unwanted explanatory text: '{phrase}'"
    return True, None


def save_to_cache(calendar: str, date_str: str) -> None:
    """Cache successful calendar for fallback."""
    try:
        CACHE_FILE.write_text(json.dumps({
            "date": date_str,
            "content": calendar,
            "cached_at": datetime.now().isoformat()
        }))
        logger.info("Calendar cached successfully")
    except Exception as e:
        logger.warning(f"Failed to cache calendar: {e}")


def load_from_cache() -> dict | None:
    """Load cached calendar if available."""
    try:
        if CACHE_FILE.exists():
            return json.loads(CACHE_FILE.read_text())
    except Exception as e:
        logger.warning(f"Failed to load cache: {e}")
    return None


# ============= EARNINGS (Nasdaq API - deterministic) =============
def parse_market_cap(cap_str: str) -> float:
    """Parse Nasdaq market cap string like '$1,234,567,890' to float."""
    if not cap_str or cap_str == "N/A" or cap_str == "":
        return 0.0
    try:
        return float(cap_str.replace("$", "").replace(",", ""))
    except (ValueError, AttributeError):
        return 0.0


@retry_with_backoff(max_retries=3, base_delay=2, exceptions=(requests.RequestException,))
def fetch_earnings(date_str: str) -> str:
    """Fetch earnings from Nasdaq API and format for Slack."""
    logger.info(f"Fetching earnings from Nasdaq API for {date_str}")

    response = requests.get(
        NASDAQ_EARNINGS_URL,
        params={"date": date_str},
        headers=NASDAQ_HEADERS,
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    data = response.json()

    rows = data.get("data", {}).get("rows", [])
    logger.info(f"Nasdaq API returned {len(rows)} companies")

    if not rows:
        return "*Earnings:*\n• No major earnings scheduled"

    before_market = []
    after_market = []

    for row in rows:
        ticker = (row.get("symbol") or "").strip()
        market_cap = parse_market_cap(row.get("marketCap", ""))
        time_label = (row.get("time") or "").strip().lower()

        # Include if market cap > $1B OR if on watchlist
        is_watchlist = ticker.upper() in WATCHLIST
        if market_cap < MIN_MARKET_CAP and not is_watchlist:
            continue

        # Ticker only, bolded if watchlist
        if is_watchlist:
            entry = f"*{ticker}*"
        else:
            entry = ticker

        sort_key = market_cap

        if time_label == "time-pre-market":
            before_market.append((sort_key, entry))
        else:
            after_market.append((sort_key, entry))

    # Sort by market cap descending
    before_market.sort(key=lambda x: x[0], reverse=True)
    after_market.sort(key=lambda x: x[0], reverse=True)

    # Take top N, but force-include watchlist tickers that didn't make the cut
    def top_with_watchlist(items):
        top = items[:MAX_PER_SECTION]
        top_entries = {e for _, e in top}
        for _, entry in items[MAX_PER_SECTION:]:
            if entry.startswith("*") and entry not in top_entries:
                top.append((0, entry))
        return [e for _, e in top]

    before_market = top_with_watchlist(before_market)
    after_market = top_with_watchlist(after_market)

    # Log watchlist hits
    all_entries = before_market + after_market
    watchlist_hits = [e for e in all_entries if e.startswith("*")]
    if watchlist_hits:
        logger.info(f"Watchlist hits: {', '.join(watchlist_hits)}")

    lines = ["*Earnings:*"]
    if before_market:
        lines.append(f"• Before Market: {', '.join(before_market)}")
    if after_market:
        lines.append(f"• After Market: {', '.join(after_market)}")
    if not before_market and not after_market:
        lines.append("• No major earnings scheduled")

    return "\n".join(lines)


# ============= ECONOMIC CALENDAR (Claude API) =============
def _call_claude_api(prompt: str) -> str | None:
    """Make a single Claude API call and return cleaned text."""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    message = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1500,
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
        messages=[{"role": "user", "content": prompt}]
    )

    text = "".join(
        block.text for block in message.content if block.type == "text"
    )

    if not text:
        logger.error("No text content in API response")
        return None

    # Clean up: find the Economic Data section
    for marker in ["*Economic Data:*", "Economic Data:"]:
        if marker in text:
            idx = text.index(marker)
            text = text[idx:]
            break

    # Ensure it starts correctly
    if not text.startswith("*Economic Data:*"):
        text = "*Economic Data:*\n" + text

    # Convert markdown bold (**) to Slack bold (*)
    text = text.replace("**", "*")

    return text


@retry_with_backoff(max_retries=3, base_delay=2, exceptions=(anthropic.APIError, anthropic.APIConnectionError))
def fetch_economic_calendar(tomorrow: datetime) -> str | None:
    """Fetch economic calendar using Claude API with content quality retries."""
    today = datetime.now()

    today_str = today.strftime("%A, %B %d, %Y")
    today_weekday = today.strftime("%A")
    tomorrow_str = tomorrow.strftime("%A, %B %d, %Y")
    tomorrow_weekday = tomorrow.strftime("%A")
    tomorrow_short = tomorrow.strftime("%A, %b %d, %Y")
    tomorrow_search = tomorrow.strftime("%B %d %Y")

    prompt = ECON_PROMPT_TEMPLATE.format(
        today_date=today_str,
        today_weekday=today_weekday,
        tomorrow_date=tomorrow_str,
        tomorrow_weekday=tomorrow_weekday,
        tomorrow_date_short=tomorrow_short,
        tomorrow_date_search=tomorrow_search
    )

    logger.info(f"Fetching economic calendar for: {tomorrow_str}")

    last_error = None
    for attempt in range(MAX_CONTENT_RETRIES + 1):
        if attempt > 0:
            logger.warning(f"Content quality retry {attempt}/{MAX_CONTENT_RETRIES} (reason: {last_error})")

        econ_text = _call_claude_api(prompt)

        if not econ_text:
            last_error = "Empty response from API"
            continue

        is_valid, error = validate_economic_data(econ_text)
        if is_valid:
            logger.info(f"Economic calendar fetched successfully on attempt {attempt + 1}")
            return econ_text
        else:
            last_error = error
            logger.warning(f"Validation failed on attempt {attempt + 1}: {error}")

    logger.error(f"All {MAX_CONTENT_RETRIES + 1} economic calendar attempts failed. Last error: {last_error}")
    return None


# ============= COMBINED OUTPUT =============
def build_slack_message(economic_data: str, earnings_data: str, date_short: str) -> str:
    """Combine economic calendar and earnings into one Slack message."""
    return f"📊 US & Canada Market Calendar - {date_short}\n\n{economic_data}\n\n{earnings_data}"


# ============= SLACK =============
@retry_with_backoff(max_retries=3, base_delay=1, exceptions=(requests.RequestException,))
def post_to_slack(message: str) -> bool:
    """Post message to Slack via webhook."""
    response = requests.post(
        SLACK_WEBHOOK_URL,
        json={
            "text": message,
            "unfurl_links": False,
            "unfurl_media": False
        },
        headers={'Content-Type': 'application/json'},
        timeout=REQUEST_TIMEOUT
    )
    if response.status_code == 200:
        logger.info("Posted to Slack successfully")
        return True
    else:
        logger.error(f"Slack webhook error: {response.status_code} - {response.text}")
        return False


# ============= MAIN =============
def main(dry_run: bool = False, use_cache: bool = False) -> int:
    """Main execution function. Returns exit code."""
    logger.info("=" * 50)
    logger.info("Economic Calendar + Earnings Bot - Starting")
    logger.info(f"Mode: {'DRY RUN' if dry_run else 'LIVE'}")
    logger.info("=" * 50)

    # Verify configuration
    if not ANTHROPIC_API_KEY:
        logger.error("ANTHROPIC_API_KEY not found in environment")
        return 1
    if not SLACK_WEBHOOK_URL and not dry_run:
        logger.error("SLACK_WEBHOOK_URL not found in environment")
        return 1

    tomorrow = get_next_trading_day()
    tomorrow_short = tomorrow.strftime("%A, %b %d, %Y")
    tomorrow_api = tomorrow.strftime("%Y-%m-%d")

    # === FETCH BOTH IN PARALLEL (sequential here, but independent) ===

    # 1. Economic Calendar (Claude API)
    economic_data = None
    if use_cache:
        cached = load_from_cache()
        if cached:
            logger.info(f"Using cached data from {cached.get('cached_at', 'unknown')}")
            # Post cached full message as-is
            if dry_run:
                print("\n" + "=" * 50)
                print("DRY RUN (cached) - Would post to Slack:")
                print("=" * 50 + "\n")
                print(cached["content"])
                return 0
            return 0 if post_to_slack(cached["content"]) else 1

    try:
        economic_data = fetch_economic_calendar(tomorrow)
    except Exception as e:
        logger.error(f"Failed to fetch economic calendar: {e}")

    if not economic_data:
        logger.warning("Economic calendar failed - using fallback")
        economic_data = "*Economic Data:*\n• Unable to fetch economic data"

    # 2. Earnings (Nasdaq API - deterministic)
    earnings_data = None
    try:
        earnings_data = fetch_earnings(tomorrow_api)
    except Exception as e:
        logger.error(f"Failed to fetch earnings from Nasdaq: {e}")

    if not earnings_data:
        logger.warning("Earnings fetch failed - using fallback")
        earnings_data = "*Earnings:*\n• Unable to fetch earnings data"

    # === COMBINE ===
    full_message = build_slack_message(economic_data, earnings_data, tomorrow_short)

    # Cache successful result
    save_to_cache(full_message, tomorrow.strftime("%A, %B %d, %Y"))

    # Dry run - just print
    if dry_run:
        print("\n" + "=" * 50)
        print("DRY RUN - Would post to Slack:")
        print("=" * 50 + "\n")
        print(full_message)
        print("\n" + "=" * 50)
        return 0

    # Post to Slack
    try:
        success = post_to_slack(full_message)
    except Exception as e:
        logger.error(f"Failed to post to Slack: {e}")
        return 1

    if success:
        logger.info("SUCCESS: Calendar + Earnings posted to Slack")
        return 0
    else:
        logger.error("FAILED: Could not post to Slack")
        return 1


if __name__ == "__main__":
    dry_run = "--dry-run" in sys.argv or "-d" in sys.argv
    use_cache = "--cache" in sys.argv or "-c" in sys.argv

    if "--help" in sys.argv or "-h" in sys.argv:
        print("""
Economic Calendar + Earnings Bot

Usage: python economic_calendar_bot.py [OPTIONS]

Options:
  --dry-run, -d    Fetch calendar but don't post to Slack (prints to console)
  --cache, -c      Use cached calendar instead of fetching new one
  --help, -h       Show this help message

Environment Variables (in .env file):
  ANTHROPIC_API_KEY    Your Anthropic API key
  SLACK_WEBHOOK_URL    Slack incoming webhook URL
        """)
        sys.exit(0)

    exit_code = main(dry_run=dry_run, use_cache=use_cache)
    sys.exit(exit_code)

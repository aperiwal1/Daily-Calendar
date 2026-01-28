#!/usr/bin/env python3
"""
Daily Economic Calendar Bot
Posts US & Canada economic events and earnings to Slack
"""

import os
import sys
import json
import logging
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

# ============= PROMPT =============
PROMPT_TEMPLATE = """Search for {tomorrow_date}'s US and Canada economic calendar and earnings.

SEARCH STRATEGY (do all 3 searches):
1. Search: "US economic calendar {tomorrow_date_short}"
2. Search: "Canada economic calendar {tomorrow_date_short}" OR "StatCan releases {tomorrow_date_short}"
3. Search: "earnings calendar {tomorrow_date_short}" - use Nasdaq.com/market-activity/earnings

EARNINGS VERIFICATION:
- Use Nasdaq.com/market-activity/earnings as authority for timing
- "BMO" = Before Market, "AMC" = After Market
- Tech giants (AAPL, AMZN, META, GOOGL, MSFT) almost always report AFTER close
- ONLY include companies with market cap > $1 Billion
- If a company's market cap is unknown or unclear, exclude it
- Sort earnings by market cap (largest first within each section)

OUTPUT THIS EXACT FORMAT:

📊 US & Canada Market Calendar - {tomorrow_date_short}

*Economic Data:*
• [Time] ET: 🇺🇸 [US Event]
• [Time] ET: 🇺🇸 [US Event]
• [Time] ET: 🇨🇦 [Canada Event]

*Earnings:*
• Before Market: Company (TICKER), Company (TICKER)
• After Market: Company (TICKER), Company (TICKER)

STRICT RULES:
1. EVERY economic event gets its own bullet point - never combine multiple events on one line
2. EVERY economic event MUST have a country flag: 🇺🇸 for US, 🇨🇦 for Canada
3. Output ONLY the formatted calendar - no preamble, notes, explanations, sources
4. Search for Canada data (StatCan, BoC) - if none scheduled, don't include any
5. If no economic data: • No major releases scheduled
6. If no earnings: • No major earnings scheduled
7. Use abbreviations: CPI, PPI, GDP, PCE, PMI, BoC, FOMC
8. EARNINGS FILTER: Only companies with market cap > $1 Billion - exclude smaller companies
9. Max 8 earnings per section (Before/After Market), sorted by market cap (largest first)
10. Sort economic events by time
11. Start with 📊 - no text before it

EXAMPLE OUTPUT:
📊 US & Canada Market Calendar - Thursday, Jan 29, 2026

*Economic Data:*
• 08:30 ET: 🇺🇸 GDP Q4 Advance
• 08:30 ET: 🇺🇸 Initial Jobless Claims
• 08:30 ET: 🇺🇸 PCE Price Index (Dec)
• 08:30 ET: 🇨🇦 GDP (Nov)
• 10:00 ET: 🇺🇸 Pending Home Sales (Dec)

*Earnings:*
• Before Market: Mastercard (MA), Caterpillar (CAT)
• After Market: Apple (AAPL), Visa (V), Intel (INTC)"""


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
    """Return the next trading day, skipping weekends.
    
    - Mon-Thu: returns next day
    - Friday: returns Monday
    - Sat: returns Monday
    - Sun: returns Monday
    """
    today = datetime.now()
    days_ahead = 1
    
    # If Friday (4), skip to Monday (add 3 days)
    if today.weekday() == 4:
        days_ahead = 3
    # If Saturday (5), skip to Monday (add 2 days)
    elif today.weekday() == 5:
        days_ahead = 2
    # If Sunday (6), skip to Monday (add 1 day)
    elif today.weekday() == 6:
        days_ahead = 1
    
    next_day = today + timedelta(days=days_ahead)
    logger.info(f"Today is {today.strftime('%A')} - next trading day: {next_day.strftime('%A, %b %d')}")
    
    return next_day


def validate_calendar(text: str) -> tuple[bool, str | None]:
    """Validate that the calendar response has expected structure."""
    if not text:
        return False, "Empty response"
    
    if len(text) < 50:
        return False, f"Response too short ({len(text)} chars)"
    
    # Must start with the emoji (no preamble)
    if not text.strip().startswith("📊"):
        return False, "Response doesn't start with 📊 (has preamble)"
    
    # Check for required sections
    if "*Economic Data:*" not in text and "Economic Data:" not in text:
        return False, "Missing Economic Data section"
    
    if "*Earnings:*" not in text and "Earnings:" not in text:
        return False, "Missing Earnings section"
    
    # Check for unwanted content (explanations, notes)
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


# ============= CORE FUNCTIONS =============
@retry_with_backoff(max_retries=3, base_delay=2, exceptions=(anthropic.APIError, anthropic.APIConnectionError))
def get_tomorrow_calendar() -> str | None:
    """Fetch tomorrow's economic calendar using Claude API."""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    
    today = datetime.now()
    tomorrow = get_next_trading_day()
    today_str = today.strftime("%A, %B %d, %Y")
    tomorrow_str = tomorrow.strftime("%A, %B %d, %Y")
    tomorrow_short = tomorrow.strftime("%A, %b %d, %Y")  # "Wednesday, Jan 28, 2026"
    
    prompt = PROMPT_TEMPLATE.format(
        today_date=today_str, 
        tomorrow_date=tomorrow_str,
        tomorrow_date_short=tomorrow_short
    )
    
    logger.info(f"Today: {today_str}")
    logger.info(f"Fetching calendar for: {tomorrow_str}")
    
    message = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=2000,
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
        messages=[{"role": "user", "content": prompt}]
    )
    
    # Extract text from response
    calendar_text = "".join(
        block.text for block in message.content if block.type == "text"
    )
    
    if not calendar_text:
        logger.error("No text content in API response")
        return None
    
    # Clean up: Start from the calendar header
    for marker in ["📊", "US & Canada Market Calendar"]:
        if marker in calendar_text:
            idx = calendar_text.index(marker)
            calendar_text = calendar_text[idx:]
            if not calendar_text.startswith("📊"):
                calendar_text = "📊 " + calendar_text
            break
    
    # Convert markdown bold (**) to Slack bold (*)
    calendar_text = calendar_text.replace("**", "*")
    
    # Validate response
    is_valid, error = validate_calendar(calendar_text)
    if not is_valid:
        logger.error(f"Calendar validation failed: {error}")
        return None
    
    logger.info(f"Calendar fetched successfully ({len(calendar_text)} chars)")
    
    # Cache successful result
    save_to_cache(calendar_text, tomorrow_str)
    
    return calendar_text


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


def main(dry_run: bool = False, use_cache: bool = False) -> int:
    """Main execution function. Returns exit code."""
    logger.info("=" * 50)
    logger.info("Economic Calendar Bot - Starting")
    logger.info(f"Mode: {'DRY RUN' if dry_run else 'LIVE'}")
    logger.info("=" * 50)
    
    # Verify configuration
    if not ANTHROPIC_API_KEY:
        logger.error("ANTHROPIC_API_KEY not found in environment")
        return 1
    
    if not SLACK_WEBHOOK_URL and not dry_run:
        logger.error("SLACK_WEBHOOK_URL not found in environment")
        return 1
    
    # Get calendar
    calendar = None
    
    if use_cache:
        cached = load_from_cache()
        if cached:
            logger.info(f"Using cached calendar from {cached.get('cached_at', 'unknown')}")
            calendar = cached["content"]
    
    if not calendar:
        try:
            calendar = get_tomorrow_calendar()
        except Exception as e:
            logger.error(f"Failed to fetch calendar: {e}")
            
            # Try fallback to cache
            cached = load_from_cache()
            if cached:
                logger.warning("Using stale cached calendar as fallback")
                calendar = f"⚠️ _Using cached data from {cached.get('date', 'unknown')}_\n\n{cached['content']}"
    
    if not calendar:
        logger.error("Failed to fetch calendar and no cache available")
        return 1
    
    # Dry run - just print
    if dry_run:
        print("\n" + "=" * 50)
        print("DRY RUN - Would post to Slack:")
        print("=" * 50 + "\n")
        print(calendar)
        print("\n" + "=" * 50)
        return 0
    
    # Post to Slack
    try:
        success = post_to_slack(calendar)
    except Exception as e:
        logger.error(f"Failed to post to Slack: {e}")
        return 1
    
    if success:
        logger.info("SUCCESS: Calendar posted to Slack")
        return 0
    else:
        logger.error("FAILED: Could not post to Slack")
        return 1


if __name__ == "__main__":
    # Parse command line arguments
    dry_run = "--dry-run" in sys.argv or "-d" in sys.argv
    use_cache = "--cache" in sys.argv or "-c" in sys.argv
    
    if "--help" in sys.argv or "-h" in sys.argv:
        print("""
Economic Calendar Bot

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

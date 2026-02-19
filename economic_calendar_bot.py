#!/usr/bin/env python3
"""
Daily Economic Calendar Bot
Posts US & Canada economic events and earnings to Slack
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
MAX_CONTENT_RETRIES = 2  # Retries specifically for lazy/bad content

# Priority watchlist tickers to bold in output
WATCHLIST_US = ["TSLA", "NVDA", "AMZN", "AAPL", "META", "MSFT", "PLTR", "GOOG", "GOOGL", 
                "AMD", "IREN", "SOFI", "NFLX", "MSTR", "BRK.B", "RKLB", "AVGO", "TSM", 
                "MU", "HOOD", "NBIS", "ASTS"]
WATCHLIST_CAD = ["ENB", "SHOP", "TD", "RY", "T", "BNS", "BCE", "IAG", "CNQ", "CM", 
                 "POW", "BMO", "DOL", "CLS", "PSLV", "WCP", "CSU", "SU", "SCZ", "BN", "CNR"]
WATCHLIST_ALL = WATCHLIST_US + WATCHLIST_CAD + [f"{t}.TO" for t in WATCHLIST_CAD]

# Phrases that indicate the model was lazy instead of listing specifics
LAZY_PHRASES = [
    "multiple companies scheduled",
    "total earnings expected",
    "various companies",
    "several companies",
    "numerous companies",
    "companies are scheduled",
    "companies scheduled",
    "earnings are expected",
    "companies reporting",
    "additional companies",
    "and others",
    "and more",
    "among others",
    "plus more",
    "many companies",
    "dozens of companies",
]

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
PROMPT_TEMPLATE = """
CRITICAL CONTEXT:
- Today is {today_date} ({today_weekday})
- You are searching for: {tomorrow_date_short} ({tomorrow_weekday})
- If today is Friday, you are looking for MONDAY's calendar - search accordingly

Search for {tomorrow_date}'s US and Canada economic calendar and earnings.

SEARCH STRATEGY (do all searches):
1. Search: "US economic calendar {tomorrow_date_search}"
2. Search: "Canada economic calendar {tomorrow_date_search}" OR "StatCan releases {tomorrow_date_search}"
3. Search: "Nasdaq earnings calendar {tomorrow_date_search}"
4. Search: "TMX earnings calendar {tomorrow_date_search}" for Canadian earnings

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

PRIORITY WATCHLIST - MUST CHECK EACH ONE:
These tickers MUST be checked for earnings on {tomorrow_date_short}. If any are reporting, include them.

US Tickers (check on Nasdaq.com):
TSLA, NVDA, AMZN, AAPL, META, MSFT, PLTR, GOOG, GOOGL, AMD, IREN, SOFI, NFLX, MSTR, BRK.B, RKLB, AVGO, TSM, MU, HOOD, NBIS, ASTS

Canadian Tickers (check on TMXMoney.com or Yahoo Finance with .TO suffix):
ENB, SHOP, TD, RY, T, BNS, BCE, IAG, CNQ, CM, POW, BMO, DOL, CLS, PSLV, WCP, CSU, SU, SCZ, BN, CNR

For Canadian tickers, search "[TICKER].TO earnings date" or "TMX [COMPANY NAME] earnings"

EARNINGS SEARCH STRATEGY:
1. Search: "Nasdaq earnings calendar {tomorrow_date_search}" - this shows market cap for each company
2. Search: "site:nasdaq.com/market-activity/earnings {tomorrow_date_search}"
3. Search: "TMX earnings calendar {tomorrow_date_search}" for Canadian stocks
4. Include ALL companies with market cap > $1 Billion - aim for 10-15 companies per section if available
5. Do NOT be conservative - if a $1B+ company appears on any earnings calendar for this date, include it

EARNINGS RULES:
- US: Nasdaq.com/market-activity/earnings is the authority (shows market cap)
- Canada: TMXMoney.com or Yahoo Finance ([TICKER].TO)
- "BMO" = Before Market, "AMC" = After Market
- Tech giants (AAPL, AMZN, META, GOOGL, MSFT, NVDA) almost always report AFTER close
- List ALL companies > $1B market cap, not just the top few
- Sort by market cap (largest first)
- Mark Canadian stocks with 🇨🇦 flag
- Watchlist tickers are pre-qualified - always include if reporting

═══════════════════════════════════════════════════
ABSOLUTE RULES — VIOLATIONS WILL BE REJECTED
═══════════════════════════════════════════════════
NEVER use summary or placeholder language for earnings. These phrases are BANNED:
- "Multiple companies scheduled"
- "X total earnings expected"
- "Various/several/numerous companies"
- "Companies are scheduled"
- "And others" / "and more" / "among others" / "plus more"
- ANY form of summarizing instead of listing actual company names

You MUST list each company by name and ticker, or say "No major earnings scheduled".
There is NO middle ground. Summarizing = failure.
═══════════════════════════════════════════════════

VALIDATION CHECK - READ THIS:
- Mondays typically have 5+ earnings from $1B+ companies - an empty Monday is almost NEVER correct
- First trading day of the month usually has ISM Manufacturing PMI at 10:00 AM ET
- If your initial search returns "no major releases" or "no earnings", SEARCH AGAIN with different queries
- Try: "earnings reports {tomorrow_date_search}", "companies reporting earnings {tomorrow_date_search}"
- Check PLTR, GOOG, AMD, DIS specifically if searching for a Monday in early February
- If it's Thursday and Initial Jobless Claims is missing — you made an error, search again

OUTPUT THIS EXACT FORMAT:

📊 US & Canada Market Calendar - {tomorrow_date_short}

*Economic Data:*
• [Time] ET: 🇺🇸 [US Event]
• [Time] ET: 🇨🇦 [Canada Event]

*Earnings:*
• Before Market: Company (TICKER), 🇨🇦 Company (TICKER.TO)
• After Market: Company (TICKER), 🇨🇦 Company (TICKER.TO)

STRICT RULES:
1. EVERY economic event gets its own bullet point - never combine multiple events on one line
2. EVERY economic event MUST have a country flag: 🇺🇸 for US, 🇨🇦 for Canada - NO EXCEPTIONS
3. SORT ECONOMIC DATA BY TIME - earliest first (08:30 before 09:45 before 10:00) - THIS IS MANDATORY
4. Output ONLY the formatted calendar - no preamble, notes, explanations, sources
5. Search for Canada data (StatCan, BoC) - if none scheduled, don't include any
6. If genuinely no economic data after multiple searches: • No major releases scheduled
7. If genuinely no earnings after multiple searches: • No major earnings scheduled
8. Use abbreviations: CPI, PPI, GDP, PCE, PMI, BoC, FOMC
9. EARNINGS: Include ALL companies > $1B market cap reporting that day (aim for 10-15 per section)
10. WATCHLIST PRIORITY: Always check and include watchlist tickers if reporting - never miss these
11. Canadian earnings: Add 🇨🇦 flag before company name and use .TO suffix
12. Max 15 earnings per section (Before/After Market), sorted by market cap (largest first)
13. Sort economic events by time STRICTLY ASCENDING (e.g., 08:30, 08:30, 09:45, 10:00, 11:45)
14. Start with 📊 - no text before it
15. List SPECIFIC company names and tickers for earnings — NEVER summarize or use placeholder counts
16. If you cannot find specific earnings names, say "No major earnings scheduled" — do NOT fabricate or summarize

EXAMPLE OUTPUT (note time order and flags):
📊 US & Canada Market Calendar - Monday, Feb 02, 2026

*Economic Data:*
• 08:30 ET: 🇺🇸 Initial Jobless Claims (week ending Jan 24)
• 10:00 ET: 🇺🇸 ISM Manufacturing PMI (Jan)
• 10:00 ET: 🇺🇸 Construction Spending (Dec)

*Earnings:*
• Before Market: Palantir (PLTR), Toyota (TM), Clorox (CLX), 🇨🇦 Royal Bank (RY.TO)
• After Market: Alphabet (GOOG), AMD (AMD), Disney (DIS), Amgen (AMGN)"""


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


def bold_watchlist_tickers(text: str) -> str:
    """Bold any watchlist tickers in the calendar text for Slack."""
    for ticker in WATCHLIST_ALL:
        # Match ticker in parentheses: (AAPL) or (SHOP.TO)
        # Avoid double-bolding if already bolded
        pattern = rf'\((?<!\*)({re.escape(ticker)})(?!\*)\)'
        replacement = rf'(*\1*)'
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    
    return text


def contains_lazy_content(text: str) -> tuple[bool, str | None]:
    """Check if the response contains lazy summary language instead of specifics."""
    text_lower = text.lower()
    for phrase in LAZY_PHRASES:
        if phrase in text_lower:
            return True, phrase
    
    # Check for patterns like "160 total earnings" or "200+ companies"
    count_pattern = re.search(r'\d+\s*(total|companies|earnings)\s*(expected|scheduled|reporting)', text_lower)
    if count_pattern:
        return True, count_pattern.group(0)
    
    return False, None


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
    
    # Check for lazy summary language
    is_lazy, lazy_phrase = contains_lazy_content(text)
    if is_lazy:
        return False, f"Contains lazy summary language: '{lazy_phrase}'"
    
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
def _call_claude_api(prompt: str) -> str | None:
    """Make a single Claude API call and return cleaned text."""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    
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
    
    # Bold watchlist tickers
    calendar_text = bold_watchlist_tickers(calendar_text)
    
    return calendar_text


@retry_with_backoff(max_retries=3, base_delay=2, exceptions=(anthropic.APIError, anthropic.APIConnectionError))
def get_tomorrow_calendar() -> str | None:
    """Fetch tomorrow's economic calendar using Claude API with content quality retries."""
    today = datetime.now()
    tomorrow = get_next_trading_day()
    
    # Multiple date formats for different purposes
    today_str = today.strftime("%A, %B %d, %Y")
    today_weekday = today.strftime("%A")
    tomorrow_str = tomorrow.strftime("%A, %B %d, %Y")
    tomorrow_weekday = tomorrow.strftime("%A")
    tomorrow_short = tomorrow.strftime("%A, %b %d, %Y")
    tomorrow_search = tomorrow.strftime("%B %d %Y")
    
    prompt = PROMPT_TEMPLATE.format(
        today_date=today_str,
        today_weekday=today_weekday,
        tomorrow_date=tomorrow_str,
        tomorrow_weekday=tomorrow_weekday,
        tomorrow_date_short=tomorrow_short,
        tomorrow_date_search=tomorrow_search
    )
    
    logger.info(f"Today: {today_str} ({today_weekday})")
    logger.info(f"Fetching calendar for: {tomorrow_str} ({tomorrow_weekday})")
    
    # Try up to MAX_CONTENT_RETRIES+1 times for quality content
    last_error = None
    for attempt in range(MAX_CONTENT_RETRIES + 1):
        if attempt > 0:
            logger.warning(f"Content quality retry {attempt}/{MAX_CONTENT_RETRIES} (reason: {last_error})")
        
        calendar_text = _call_claude_api(prompt)
        
        if not calendar_text:
            last_error = "Empty response from API"
            continue
        
        # Validate response
        is_valid, error = validate_calendar(calendar_text)
        if is_valid:
            logger.info(f"Calendar fetched successfully ({len(calendar_text)} chars) on attempt {attempt + 1}")
            save_to_cache(calendar_text, tomorrow_str)
            return calendar_text
        else:
            last_error = error
            logger.warning(f"Calendar validation failed on attempt {attempt + 1}: {error}")
    
    # All retries exhausted — log but return last attempt if it exists
    logger.error(f"All {MAX_CONTENT_RETRIES + 1} content attempts failed. Last error: {last_error}")
    return None


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

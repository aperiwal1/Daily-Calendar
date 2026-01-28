name: Economic Calendar Bot

on:
  schedule:
    # 4:55 PM ET = 21:55 UTC (standard time) or 20:55 UTC (daylight saving)
    # Using 21:55 UTC - adjust if needed for daylight saving
    - cron: '55 21 * * 1-5'
  
  # Allow manual trigger for testing
  workflow_dispatch:

jobs:
  post-calendar:
    runs-on: ubuntu-latest
    
    steps:
      - name: Checkout repository
        uses: actions/checkout@v4
      
      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.11'
      
      - name: Install dependencies
        run: |
          python -m pip install --upgrade pip
          pip install -r requirements.txt
      
      - name: Run Economic Calendar Bot
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
          SLACK_WEBHOOK_URL: ${{ secrets.SLACK_WEBHOOK_URL }}
        run: python economic_calendar_bot.py

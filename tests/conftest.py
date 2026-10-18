"""Pin the test suite to the in-memory backend.

Real env vars outrank `.env` in pydantic-settings, so setting this before the
app is imported means a developer's live Sheets configuration can never make
the tests hit (or write to) the real spreadsheet.
"""

import os

# These are *set*, not popped: popping only removes the process env var and lets
# the real `.env` supply a value, which is how a live VAPI_SECRET once made the
# whole suite 401. An explicit empty value overrides the file.
os.environ["BACKEND"] = "memory"
os.environ["VAPI_SECRET"] = ""
os.environ["REQUIRE_SECRET"] = "false"
os.environ["BUSINESS_HOURS_MODE"] = "auto"
os.environ["GOOGLE_CREDENTIALS_FILE"] = ""
os.environ["GOOGLE_CREDENTIALS_JSON"] = ""
os.environ["SHEET_ID"] = ""
# Without this the suite posts real messages into the team's Slack channel —
# the same trap as the live VAPI_SECRET: unset the variable and `.env` fills
# it back in. Tests that need the notifier set it themselves.
os.environ["SLACK_WEBHOOK_URL"] = ""

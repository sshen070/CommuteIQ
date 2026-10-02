import database
import os

# One rule for every setting: a value in .env wins; otherwise the answer saved by
# setup_locations.py is used. Setup only asks about what .env leaves out.

# Google's free requests per calendar month, per SKU (Compute Routes Pro, Text
# Search Pro). Requests past this are refused, so Google usage never bills
google_monthly_limit: int = 5000
max_weekdays_per_month: int = 23

# Through GOOGLE_CREDITS_UNTIL (last day of the Google Cloud trial), Routes requests
# past the free 5,000 are paid from the $300 trial credits, up to this many a month
# ($100 at most). The default windows need up to 14,904 in a 23-weekday month, which
# comes to ~$265 for a trial running Oct 2 to Dec 30
google_credit_monthly_limit: int = 15000
google_price_per_1000: float = 10.0     # Compute Routes Pro, past the free 5,000

# TomTom answers 429 past its free monthly allowance, so samples would be lost until it resets
tomtom_free_per_month: int = 20000
max_days_per_month: int = 31


# Stripped value, or None if unset or blank
def env(name: str) -> str | None:
    value = (os.getenv(name) or "").strip()
    return value or None


# Env values are strings, so "false" must be parsed, not truth-tested
def parse_bool(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes", "y", "on")


# Toll setting plus where it came from, for the startup log
def allow_tolls(conn) -> tuple[bool, str]:
    if (value := env("TOLL")) is not None:
        return parse_bool(value), "TOLL in .env"

    if (saved := database.get_setting(conn, "allow_tolls")) is not None:
        return saved == "1", "saved by setup"

    return False, "default"

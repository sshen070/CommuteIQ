from data_extraction import fetch_google_route, fetch_route
from datetime import date, datetime, timedelta
from dotenv import load_dotenv
from google_route import GoogleRoute
from trip_route import TripRoute

import config
import ctypes
import database
import logging
import os
import re
import sys
import time

# Minutes between samples; samples land on clock-aligned slots (:00, :05, :10, ...)
interval_min: int = 5

# Longest single sleep while NTP hasn't synced the clock yet (after a power cut), so its
# correction is noticed. Once synced, each slot is one sleep
max_sleep_s: int = 30

# Linux adjtimex() returns TIME_ERROR (5) while no NTP daemon has synced the clock
time_error: int = 5
libc = ctypes.CDLL(None, use_errno=True)

# Waking more than this past a slot skips it instead of sampling off the mark
late_limit_s: int = 30

# START_AT in .env: 24-hour local HH:MM
hh_mm = r"([01]?\d|2[0-3]):([0-5]\d)"
start_at_pattern = re.compile(rf"^{hh_mm}$")

# Commute windows per direction, shared by TomTom and Google so their samples pair up.
# Override in .env with WINDOW_HOME_TO_UNI / WINDOW_UNI_TO_HOME as local HH:MM-HH:MM,
# or "off" to stop sampling that direction. The defaults cover 05:00-21:00 with 216
# slots a day, which fits both free tiers: up to 13,392 TomTom requests (with toll
# routes, 31-day month) and 4,968 Google requests (weekdays, 23-weekday month)
default_windows: dict[tuple[str, str], str] = {
    ("HOME", "UNI"): "05:00-14:00",
    ("UNI", "HOME"): "12:00-21:00",
}
window_pattern = re.compile(rf"^{hh_mm}\s*-\s*{hh_mm}$")

# Google queries per direction and slot, as (traffic model, tolls allowed); each is one
# request. BEST_GUESS = Google's estimate from historical and live traffic, the realistic
# number to set against TomTom. PESSIMISTIC ran 15+ minutes over at peak, too loose to be
# a useful bound, so it isn't sampled. The toll query only runs while trial credits last
google_free_queries: list[tuple[str, bool]] = [("BEST_GUESS", False)]
google_credit_queries: list[tuple[str, bool]] = [("BEST_GUESS", False), ("BEST_GUESS", True)]

logger = logging.getLogger("commuteiq")


# Next clock-aligned slot strictly after now
def next_slot(now: float) -> float:
    interval_s = interval_min * 60
    return (now // interval_s + 1) * interval_s


# Next local HH:MM: today if it's still ahead, otherwise tomorrow
def next_start(start_at: str) -> float:
    hour, minute = (int(part) for part in start_at.split(":"))

    now = datetime.now()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)

    # A naive local datetime converts via the OS, so DST changes are handled
    return target.timestamp()


# Whether NTP has synced the clock since boot. The container shares the host's kernel clock,
# so this reads the host's state; a zeroed struct timex (modes = 0) only reads it and needs
# no privileges. If the call fails, report unsynced so sleeps stay short
def clock_synced() -> bool:
    timex = ctypes.create_string_buffer(512)    # bigger than struct timex
    return libc.adjtimex(timex) not in (time_error, -1)


def sleep_until(target: float):

    # Re-check after waking: a clock correction mid-sleep can end it early,
    # which would otherwise put two samples in one slot
    while (remaining := target - time.time()) > 0:
        time.sleep(remaining)


def collect_samples(conn, legs, api_key: str, allow_tolls: bool = False):

    # Always sample the toll-free route; add the toll route if the user is flexible
    toll_options = [False, True] if allow_tolls else [False]

    for origin, destination in legs:
        for tolls in toll_options:
            leg = f"{origin.role} -> {destination.role}{' (tolls)' if tolls else ''}"

            try:
                # Run query (fetch current travel time with live traffic), counted first
                database.record_request(conn, "tomtom_routing")
                route_data: dict = fetch_route(origin, destination, api_key, tolls)

                route: TripRoute = TripRoute(
                    lengthInMeters=route_data["lengthInMeters"],
                    travelTimeInSeconds=route_data["travelTimeInSeconds"],
                    trafficDelayInSeconds=route_data["trafficDelayInSeconds"],
                    trafficLengthInMeters=route_data["trafficLengthInMeters"],
                    departureTime=route_data["departureTime"],
                    arrivalTime=route_data["arrivalTime"]
                )

                # Add route stats to database table
                database.add_sample(conn, origin, destination, route, tolls)
                logger.info(f"{leg}: {route.travel_time_min} min (+{route.traffic_delay_min} min traffic)")

            # One failed sample must never stop a multi-week run; the API key is
            # part of request URLs, so redact it from the error text
            except Exception as err:
                logger.error(f"{leg} failed: {str(err).replace(api_key, '***')}")


def collect_google_samples(conn, legs, api_key: str, queries: list[tuple[str, bool]], limit: int):

    for origin, destination in legs:
        for model, tolls in queries:
            leg = f"{origin.role} -> {destination.role}{' (tolls)' if tolls else ''} [Google {model.lower()}]"

            try:
                # Past the monthly limit, skip quietly: the warning was logged when it was reached
                if (used := database.use_request(conn, "google_routes", limit)) is None:
                    continue
                if used == limit:
                    logger.warning(f"Google monthly limit reached ({limit:,} requests): no more Google samples this month")

                # Google reports no departure time, so record when the request is sent
                departure_at = datetime.now().astimezone().isoformat(timespec="seconds")

                route_data: dict = fetch_google_route(origin, destination, api_key, tolls, model)

                route: GoogleRoute = GoogleRoute(
                    duration=route_data["duration"],
                    staticDuration=route_data["staticDuration"],
                    distanceMeters=route_data.get("distanceMeters", 0),
                    trafficModel=model,
                    departureTime=departure_at
                )

                database.add_google_sample(conn, origin, destination, route, tolls)
                logger.info(f"{leg}: {route.travel_time_min} min")

            # Same rule as TomTom: log and keep collecting
            except Exception as err:
                logger.error(f"{leg} failed: {str(err).replace(api_key, '***')}")


# A direction's window as minutes after midnight [start, end), or None if "off"
def window(origin, destination) -> tuple[int, int] | None:
    name = f"WINDOW_{origin.role}_TO_{destination.role}"
    value = config.env(name) or default_windows[(origin.role, destination.role)]

    if value.lower() == "off":
        return None

    if not (match := window_pattern.match(value)):
        sys.exit(f"{name} must be 24-hour HH:MM-HH:MM, e.g. 06:00-10:00, or off (got {value!r})")

    start_h, start_m, end_h, end_m = (int(part) for part in match.groups())
    start, end = start_h * 60 + start_m, end_h * 60 + end_m

    if start >= end:
        sys.exit(f"{name} must end after it starts, within one day (got {value!r})")

    return start, end


# 390 -> "06:30"
def clock(minutes: int) -> str:
    return f"{minutes // 60:02}:{minutes % 60:02}"


# (origin, destination) pairs whose window contains this slot
def legs_in_window(windowed_legs, slot: datetime) -> list:
    minute = slot.hour * 60 + slot.minute
    return [(o, d) for o, d, w in windowed_legs if w and w[0] <= minute < w[1]]


# Slots a day inside the windows, each direction counted separately
def window_slots(windowed_legs) -> int:
    windows = [w for _, _, w in windowed_legs if w]
    return sum(start <= minute < end for start, end in windows for minute in range(0, 24 * 60, interval_min))


# Baseline weeks from .env (TOMTOM_BASELINE_WEEKS, GOOGLE_BASELINE_WEEKS): comma-separated
# local start dates, each the first of 7 days sampled around the clock
def env_weeks(name: str) -> list[date]:
    if not (value := config.env(name)):
        return []

    try:
        return sorted(date.fromisoformat(part.strip()) for part in value.split(",") if part.strip())
    except ValueError:
        sys.exit(f"{name} must be start dates like 2026-10-04,2026-11-02 (got {value!r})")


def in_baseline(day: date, week_starts: list[date]) -> bool:
    return any(start <= day < start + timedelta(days=7) for start in week_starts)


# Baseline weeks not over yet, e.g. "Mon 2026-11-02 to Sun 2026-11-08"
def describe_weeks(week_starts: list[date], today: date) -> str:
    return ", ".join(
        f"{start:%a %Y-%m-%d} to {start + timedelta(days=6):%a %Y-%m-%d}"
        for start in week_starts if start + timedelta(days=6) >= today
    )


# A local YYYY-MM-DD date from .env (GOOGLE_CREDITS_UNTIL, TOMTOM_WEEKDAYS_ONLY_UNTIL), or None
def env_date(name: str) -> date | None:
    if not (value := config.env(name)):
        return None

    try:
        return date.fromisoformat(value)
    except ValueError:
        sys.exit(f"{name} must be a date like 2026-10-06 (got {value!r})")


# Google requests past each month's free 5,000, i.e. paid from trial credits,
# optionally leaving out one month
def paid_requests(conn, skip_month: str | None = None) -> int:
    free = config.google_monthly_limit
    by_month = database.requests_by_month(conn, "google_routes")
    return sum(max(0, n - free) for month, n in by_month.items() if month != skip_month)


# Google queries and this month's request limit for a day. While trial credits last
# (through GOOGLE_CREDITS_UNTIL, and until the credit budget is used): the credit
# queries, and the free 5,000 plus what the budget has left after other months.
# Otherwise the free plan, which never bills
def google_plan(conn, day: date, credits_until: date | None) -> tuple[list[tuple[str, bool]], int]:
    budget = round(config.google_credit_budget * 1000 / config.google_price_per_1000)

    if credits_until is not None and day <= credits_until and paid_requests(conn) < budget:
        return google_credit_queries, config.google_monthly_limit + budget - paid_requests(conn, day.strftime("%Y-%m"))

    return google_free_queries, config.google_monthly_limit


# [("BEST_GUESS", True)] -> "best_guess (tolls)"
def describe(queries: list[tuple[str, bool]]) -> str:
    return ", ".join(f"{model.lower()}{' (tolls)' if tolls else ''}" for model, tolls in queries)


def main():
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    api_key = os.getenv("TOMTOM_API_KEY")
    conn = database.connect()

    home = database.get_location(conn, "HOME")
    uni = database.get_location(conn, "UNI")

    if home is None or uni is None:
        sys.exit("Home/university locations are not set. Run: python setup_locations.py")

    # Toll Road OK? (TOLL in .env wins over the answer saved by setup)
    allow_tolls, source = config.allow_tolls(conn)

    # Which file and how much data: 0 samples after a switchover means the wrong database
    sample_count = database.count_samples(conn)
    db_file = conn.execute("PRAGMA database_list").fetchone()[2]

    logger.info(f"Database: {db_file} ({sample_count:,} samples)")
    logger.info(f"Home: {home}")
    logger.info(f"Uni:  {uni}")
    logger.info(f"TomTom toll routes: {'sampled too (2 requests per direction)' if allow_tolls else 'skipped'} ({source})")

    # Both directions, each with its window
    windowed_legs = [(o, d, window(o, d)) for o, d in [(home, uni), (uni, home)]]
    all_legs = [(o, d) for o, d, _ in windowed_legs]
    slots = window_slots(windowed_legs)

    windows = ", ".join(f"{o.role} -> {d.role} {f'{clock(w[0])}-{clock(w[1])}' if w else 'off'}" for o, d, w in windowed_legs)
    logger.info(f"Windows: {windows}")

    # The single-date settings were replaced by week lists; an old one left in .env is ignored
    for old, new in [("TOMTOM_ALL_DAY_UNTIL", "TOMTOM_BASELINE_WEEKS"), ("GOOGLE_ALL_DAY_UNTIL", "GOOGLE_BASELINE_WEEKS")]:
        if config.env(old):
            logger.warning(f"{old} is no longer used and is ignored: set {new} to the start date of each baseline week")

    # TomTom: around the clock in its baseline weeks, otherwise inside the windows every day,
    # except weekends through TOMTOM_WEEKDAYS_ONLY_UNTIL (to make room in a month that has an
    # extra baseline week). Worst case is a 31-day month, plus a baseline week's overnight slots
    tomtom_weeks = env_weeks("TOMTOM_BASELINE_WEEKS")
    weekdays_only_until = env_date("TOMTOM_WEEKDAYS_ONLY_UNTIL")
    tomtom_per_slot = 2 if allow_tolls else 1
    tomtom_most = slots * tomtom_per_slot * config.max_days_per_month
    tomtom_week_extra = 7 * (24 * 60 // interval_min * len(all_legs) - slots) * tomtom_per_slot

    if upcoming := describe_weeks(tomtom_weeks, date.today()):
        logger.info(f"TomTom: around the clock {upcoming}; otherwise inside the windows every day")
    else:
        logger.info("TomTom: inside the windows every day")

    if weekdays_only_until and weekdays_only_until >= date.today():
        logger.info(f"TomTom: skipping weekends outside baseline weeks through {weekdays_only_until:%a %Y-%m-%d}")

    database.backfill_tomtom_usage(conn)
    tomtom_used = database.requests_this_month(conn, "tomtom_routing")
    tomtom_free = config.tomtom_free_per_month

    logger.info(
        f"TomTom usage: {tomtom_used:,} requests this month; windows need up to {tomtom_most:,} of {tomtom_free:,} "
        f"free a month, or {tomtom_most + tomtom_week_extra:,} in a month with a baseline week"
    )
    if tomtom_most > tomtom_free:
        logger.warning("Over TomTom's free allowance: samples will fail late in long months. Narrow the WINDOW_* settings in .env")
    elif tomtom_weeks and tomtom_most + tomtom_week_extra > tomtom_free:
        logger.warning("A month with a baseline week can go over TomTom's free allowance: samples would fail late that month")

    # Google: around the clock in its baseline weeks, otherwise weekdays inside the windows.
    # Worst case is a 23-weekday month; past the limit, requests are refused
    google_key = config.env("GOOGLE_CLOUD_API_KEY")
    google_weeks = env_weeks("GOOGLE_BASELINE_WEEKS")
    credits_until = env_date("GOOGLE_CREDITS_UNTIL")
    queries, limit = google_plan(conn, date.today(), credits_until)
    on_credits = queries is google_credit_queries

    if google_key and slots:
        used = database.requests_this_month(conn, "google_routes")
        free = config.google_monthly_limit
        logger.info(f"Google Routes: inside the windows on weekdays ({used:,} requests used this month)")

        if upcoming := describe_weeks(google_weeks, date.today()):
            per_day = 24 * 60 // interval_min * len(all_legs) * len(queries)
            logger.info(f"Google: around the clock every day {upcoming} (~{per_day:,} requests a day)")

        if on_credits:
            credit_most = slots * len(google_credit_queries) * config.max_weekdays_per_month
            spent = paid_requests(conn) * config.google_price_per_1000 / 1000
            logger.info(
                f"Google through {credits_until:%a %Y-%m-%d} (trial credits): {describe(google_credit_queries)}, "
                f"up to {credit_most:,} requests a month in the windows; ${spent:,.2f} of the "
                f"${config.google_credit_budget:,.0f} credit budget used"
            )

        free_most = slots * len(google_free_queries) * config.max_weekdays_per_month
        logger.info(f"Google {'after that' if on_credits else '(free tier)'}: {describe(google_free_queries)}, "
                    f"up to {free_most:,} requests a month, limit {free:,}")
        if free_most > free:
            logger.warning(f"Windows can need {free_most:,} free-tier Google requests in a month, over the {free:,} "
                           "limit: Google samples stop when it's reached. Narrow the WINDOW_* settings in .env")

        if used >= limit:
            logger.warning("Google limit reached: no more Google samples this month")
    else:
        logger.info(f"Google Routes: off ({'all windows off' if google_key else 'no GOOGLE_CLOUD_API_KEY in .env'})")

    # Optional delayed first sample, e.g. START_AT=00:00 so data begins on a clean day.
    # Only for an empty database: after a restart mid-collection, resume right away
    target = next_slot(time.time())

    if start_at := config.env("START_AT"):
        if not start_at_pattern.match(start_at):
            sys.exit(f"START_AT must be 24-hour HH:MM, e.g. 00:00 (got {start_at!r})")

        if sample_count:
            logger.info(f"START_AT={start_at} skipped: samples already exist, resuming at the next 5-minute mark")
        else:
            target = next_start(start_at)

    logger.info(f"First sample at {datetime.fromtimestamp(target):%a %Y-%m-%d %H:%M}")
    logger.info(f"Clock: {'synced by NTP' if clock_synced() else f'not synced by NTP yet, re-checking it every {max_sleep_s} s'}")

    while True:
        sleep_until(target)

        # The clock stepped forward past the slot: wait for the next one
        if (late := time.time() - target) > late_limit_s:
            logger.warning(f"Skipped the {datetime.fromtimestamp(target):%H:%M} slot: clock jumped {late:.0f} s past it")
            target = next_slot(time.time())
            continue

        slot = datetime.fromtimestamp(target)
        in_window = legs_in_window(windowed_legs, slot)

        # TomTom: every slot in its baseline weeks, otherwise only inside the windows
        # (and not on weekends through TOMTOM_WEEKDAYS_ONLY_UNTIL)
        all_day = in_baseline(slot.date(), tomtom_weeks)
        weekend_off = slot.weekday() >= 5 and weekdays_only_until is not None and slot.date() <= weekdays_only_until

        if all_day or not weekend_off:
            collect_samples(conn, all_legs if all_day else in_window, api_key, allow_tolls)

        # Google: every slot in its baseline weeks, otherwise weekdays inside the windows
        # (classes are Mon-Fri, and its allowance is tight)
        google_all_day = in_baseline(slot.date(), google_weeks)

        if google_key and (google_all_day or slot.weekday() < 5):
            queries, limit = google_plan(conn, slot.date(), credits_until)

            # When trial credits end (date passed or budget used), Google drops to the free plan,
            # whose limit may already be used up this month; say so once, since its requests then
            # stop without errors
            if on_credits and queries is not google_credit_queries:
                on_credits = False
                logger.info(f"Google trial credits ended: {describe(google_free_queries)} only, "
                            f"limit {config.google_monthly_limit:,} a month")

            collect_google_samples(conn, all_legs if google_all_day else in_window, google_key, queries, limit)

        target = next_slot(time.time())


if __name__ == "__main__":
    main()

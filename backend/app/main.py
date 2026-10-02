from data_extraction import fetch_google_route, fetch_route
from datetime import datetime, timedelta
from dotenv import load_dotenv
from google_route import GoogleRoute
from trip_route import TripRoute

import config
import database
import logging
import os
import re
import sys
import time

# Minutes between samples; samples land on clock-aligned slots (:00, :05, :10, ...)
interval_min: int = 5

# START_AT in .env: 24-hour local HH:MM
hh_mm = r"([01]?\d|2[0-3]):([0-5]\d)"
start_at_pattern = re.compile(rf"^{hh_mm}$")

# Google Routes is free for only 5,000 requests a month, so unlike TomTom it samples
# weekday windows, toll-free only. The defaults cover 05:00-21:00 with 216 requests per
# weekday: 4,968 in a 23-weekday month, the longest there is. Override in .env with
# GOOGLE_HOME_TO_UNI / GOOGLE_UNI_TO_HOME as local HH:MM-HH:MM, or "off"
google_windows: dict[tuple[str, str], str] = {
    ("HOME", "UNI"): "05:00-14:00",
    ("UNI", "HOME"): "12:00-21:00",
}
window_pattern = re.compile(rf"^{hh_mm}\s*-\s*{hh_mm}$")

# PESSIMISTIC = "longer than actual on most days", the upper bound to TomTom's estimate.
# Each model added (e.g. BEST_GUESS, Google's typical case) adds that many requests again
google_traffic_models: list[str] = ["PESSIMISTIC"]
max_weekdays_per_month: int = 23

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
                # Run query (fetch current travel time with live traffic)
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


# A leg's Google window as minutes after midnight [start, end), or None if "off"
def google_window(origin, destination) -> tuple[int, int] | None:
    name = f"GOOGLE_{origin.role}_TO_{destination.role}"
    value = config.env(name) or google_windows[(origin.role, destination.role)]

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


# Google requests per weekday from the windows; a month has 20-23 weekdays
def google_requests_per_weekday(google_legs) -> int:
    slots = sum(
        start <= minute < end
        for _, _, (start, end) in google_legs
        for minute in range(0, 24 * 60, interval_min)
    )
    return slots * len(google_traffic_models)


def collect_google_samples(conn, google_legs, api_key: str, slot: datetime):

    # Weekdays only: classes are Mon-Fri, and the free allowance is tight
    if slot.weekday() >= 5:
        return

    minute = slot.hour * 60 + slot.minute
    limit = config.google_monthly_limit

    for origin, destination, (start, end) in google_legs:
        if not start <= minute < end:
            continue

        for model in google_traffic_models:
            leg = f"{origin.role} -> {destination.role} [Google {model.lower()}]"

            try:
                # Past the free allowance, skip quietly: the warning was logged when it was reached
                if (used := database.use_request(conn, "google_routes", limit)) is None:
                    continue
                if used == limit:
                    logger.warning(f"Google free allowance used up ({limit:,} requests): no more Google samples this month")

                # Google reports no departure time, so record when the request is sent
                departure_at = datetime.now().astimezone().isoformat(timespec="seconds")

                # Toll-free only, to match TomTom's always-sampled toll-free route
                route_data: dict = fetch_google_route(origin, destination, api_key, False, model)

                route: GoogleRoute = GoogleRoute(
                    duration=route_data["duration"],
                    staticDuration=route_data["staticDuration"],
                    distanceMeters=route_data.get("distanceMeters", 0),
                    trafficModel=model,
                    departureTime=departure_at
                )

                database.add_google_sample(conn, origin, destination, route, False)
                logger.info(f"{leg}: {route.travel_time_min} min")

            # Same rule as TomTom: log and keep collecting
            except Exception as err:
                logger.error(f"{leg} failed: {str(err).replace(api_key, '***')}")


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
    logger.info(f"TomTom toll routes: {'sampled too (4 requests per slot)' if allow_tolls else 'skipped'} ({source})")

    # Both directions every interval
    legs = [(home, uni), (uni, home)]

    # Google as a pessimistic upper bound, within each direction's window
    google_key = config.env("GOOGLE_CLOUD_API_KEY")
    google_legs = []

    if google_key:
        google_legs = [(o, d, window) for o, d in legs if (window := google_window(o, d))]

    if google_legs:
        windows = ", ".join(f"{o.role} -> {d.role} {clock(start)}-{clock(end)}" for o, d, (start, end) in google_legs)
        models = ", ".join(model.lower() for model in google_traffic_models)
        limit = config.google_monthly_limit

        # Worst case is a 23-weekday month; beyond the limit, requests are refused, not billed
        most = google_requests_per_weekday(google_legs) * max_weekdays_per_month
        used = database.requests_this_month(conn, "google_routes")

        logger.info(f"Google Routes: {windows} on weekdays, toll-free, {models}")
        logger.info(f"Google usage: {used:,} of {limit:,} free requests this month; up to {most:,} per month at these windows")

        if used >= limit:
            logger.warning("Google free allowance used up: no more Google samples this month")
        if most > limit:
            logger.warning(
                f"Windows can need {most:,} requests in a month, over the {limit:,} limit: "
                "Google samples stop when it's reached. Narrow the GOOGLE_* windows in .env"
            )
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

    while True:
        sleep_until(target)
        collect_samples(conn, legs, api_key, allow_tolls)
        collect_google_samples(conn, google_legs, google_key, datetime.fromtimestamp(target))
        target = next_slot(time.time())


if __name__ == "__main__":
    main()

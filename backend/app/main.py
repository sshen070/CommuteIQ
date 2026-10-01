from data_extraction import fetch_route
from datetime import datetime, timedelta
from dotenv import load_dotenv
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
start_at_pattern = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")

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
    logger.info(f"Toll routes: {'sampled too (4 requests per slot)' if allow_tolls else 'skipped'} ({source})")

    # Both directions every interval
    legs = [(home, uni), (uni, home)]

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
        target = next_slot(time.time())


if __name__ == "__main__":
    main()

from data_extraction import fetch_route
from dotenv import load_dotenv
from trip_route import TripRoute

import database
import logging
import os
import sys
import time

# Minutes between samples; samples land on clock-aligned slots (:00, :05, :10, ...)
interval_min: int = 5

logger = logging.getLogger("commuteiq")


def wait_for_next_slot():
    interval_s = interval_min * 60
    target = (time.time() // interval_s + 1) * interval_s

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

    # Toll Road OK?
    allow_tolls = os.getenv("TOLL", "false").strip().lower() in ("1", "true", "yes")

    logger.info(f"Home: {home}")
    logger.info(f"Uni:  {uni}")
    logger.info(f"Toll routes: {'sampled too (4 requests per slot)' if allow_tolls else 'skipped'}")

    # Both directions every interval
    legs = [(home, uni), (uni, home)]

    while True:
        wait_for_next_slot()
        collect_samples(conn, legs, api_key, allow_tolls)


if __name__ == "__main__":
    main()

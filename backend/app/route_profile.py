from bisect import bisect_right
from data_extraction import fetch_route_sections
from datetime import datetime, timedelta
from dotenv import load_dotenv
from location import Location

import database
import logging
import math
import os
import sys

# How much of each route is freeway: its km, and its minutes on an open road. Measured once
# per home/uni pair from TomTom's route at a quiet hour, both directions, toll-free and with
# tolls (4 routing requests). Analysis uses it to turn a sample's drive time into freeway
# traffic speed, and to work out what a driver's cruising speed changes
# (analysis/driving_styles.py). The collector makes it on start; to make it without a
# restart, run from backend/app:
#   python route_profile.py
# On the server, through the container, which owns the database file:
#   sudo docker compose run --rm collector python route_profile.py

# Local hour of tomorrow that the routes leave at; TomTom's historic speeds then are open-road speeds
quiet_hour: int = 3

logger = logging.getLogger("commuteiq")


# Great-circle distance in km
def distance_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6371 * math.asin(math.sqrt(h))


# (length_m, freeway_m, travel_time_s, freeway_time_s) of a fetch_route_sections route. The
# instructions give the time to each turn; between turns, time is spread by distance
def measure(route: dict) -> tuple[int, int, int, int]:
    line = [(point["latitude"], point["longitude"]) for leg in route["legs"] for point in leg["points"]]
    along = [0.0]
    for a, b in zip(line, line[1:]):
        along.append(along[-1] + distance_km(a, b))

    total_s = route["summary"]["travelTimeInSeconds"]
    turns = sorted({(0, 0), (len(line) - 1, total_s)} | {
        (turn["pointIndex"], turn["travelTimeInSeconds"]) for turn in route["guidance"]["instructions"]
    })
    turn_km = [along[index] for index, _ in turns]
    turn_s = [seconds for _, seconds in turns]

    def seconds_at(index: int) -> float:
        km = along[index]
        i = min(max(bisect_right(turn_km, km) - 1, 0), len(turns) - 2)
        span = turn_km[i + 1] - turn_km[i]
        return turn_s[i] + (turn_s[i + 1] - turn_s[i]) * ((km - turn_km[i]) / span if span else 0)

    freeway_km = freeway_s = 0.0
    for section in route.get("sections", []):
        if section["sectionType"] == "MOTORWAY":
            start, end = section["startPointIndex"], section["endPointIndex"]
            freeway_km += along[end] - along[start]
            freeway_s += seconds_at(end) - seconds_at(start)

    return route["summary"]["lengthInMeters"], round(freeway_km * 1000), total_s, round(freeway_s)


# Makes whichever of the active pair's 4 profiles aren't stored yet. Returns whether all 4
# are stored; after a failed request the caller tries again later
def load_or_make(conn, home: Location, uni: Location, api_key: str) -> bool:
    depart = (datetime.now().astimezone() + timedelta(days=1)).replace(hour=quiet_hour, minute=0, second=0, microsecond=0)
    depart_at = depart.isoformat(timespec="seconds")
    complete = True

    for origin, destination in [(home, uni), (uni, home)]:
        for tolls in (False, True):
            if database.has_route_profile(conn, origin, destination, tolls):
                continue

            leg = f"{origin.role} -> {destination.role}{' (tolls)' if tolls else ''}"
            try:
                database.record_request(conn, "tomtom_routing")
                length_m, freeway_m, travel_s, freeway_s = measure(
                    fetch_route_sections(origin, destination, api_key, tolls, depart_at)
                )

            # The TomTom key is part of the request URL, so redact it from the error text
            except Exception as err:
                logger.error(f"Route profile {leg}: request failed: {str(err).replace(api_key, '***')}")
                complete = False
                continue

            database.add_route_profile(conn, origin, destination, tolls, length_m, freeway_m, travel_s, freeway_s, depart_at)
            logger.info(f"Route profile {leg}: {freeway_m / 1000:.1f} of {length_m / 1000:.1f} km on freeway,"
                        f" {freeway_s / 60:.0f} of {travel_s / 60:.0f} min at {quiet_hour:02}:00")

    return complete


def main():
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    conn = database.connect()
    home = database.get_location(conn, "HOME")
    uni = database.get_location(conn, "UNI")

    if home is None or uni is None:
        sys.exit("Home/university locations are not set. Run: python setup_locations.py")
    if not (api_key := os.getenv("TOMTOM_API_KEY")):
        sys.exit("No TOMTOM_API_KEY in .env")

    if not load_or_make(conn, home, uni, api_key):
        sys.exit("Some route profiles are missing; see the errors above.")


if __name__ == "__main__":
    main()

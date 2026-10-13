from conditions import store
from conditions.store import WeatherPoint
from data_extraction import check_response, routing_url, timeout
from location import Location

import database
import logging
import math
import requests

# Weather is read at fixed points along the route, so each place's readings line up over
# time. They're spaced along TomTom's toll-free route line from home to uni. The toll
# route runs within a few km of it most of the way, so both routes share the points.

# Distance between points along the route. About 14 points for a 65 km drive
spacing_km: float = 5.0

# Coordinates are rounded to 0.01° (about 1 km) before they're stored or sent to Open-Meteo
coordinate_digits: int = 2

logger = logging.getLogger("commuteiq")


# Great-circle distance in km
def distance_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6371 * math.asin(math.sqrt(h))


# The toll-free route as (lat, lon) pairs. Without live traffic, so it's the usual route
# rather than the detour of the moment. One TomTom routing request
def fetch_route_line(origin: Location, destination: Location, api_key: str) -> list[tuple[float, float]]:
    coords = f"{origin.lat},{origin.lon}:{destination.lat},{destination.lon}"
    params = {
        "key": api_key,
        "traffic": "false",
        "travelMode": "car",
        "routeType": "fastest",
        "avoid": "tollRoads",
        "routeRepresentation": "polyline",
    }

    response = requests.get(f"{routing_url}{coords}/json", params=params, timeout=timeout)
    check_response(response)

    legs = response.json()["routes"][0]["legs"]
    return [(point["latitude"], point["longitude"]) for leg in legs for point in leg["points"]]


# (route_km, lat, lon) every spacing_km along a line, starting at its first point, plus
# its last point unless one is already close to it
def spaced(line: list[tuple[float, float]]) -> list[tuple[float, float, float]]:
    points, travelled, mark = [], 0.0, 0.0

    for a, b in zip(line, line[1:]):
        step = distance_km(a, b)
        while step and mark <= travelled + step:
            f = (mark - travelled) / step
            points.append((mark, a[0] + f * (b[0] - a[0]), a[1] + f * (b[1] - a[1])))
            mark += spacing_km
        travelled += step

    if travelled - points[-1][0] > spacing_km / 2:
        points.append((travelled, *line[-1]))

    return [(round(km, 1), round(lat, coordinate_digits), round(lon, coordinate_digits)) for km, lat, lon in points]


# The active pair's points, made from the route on first use. Empty if the route
# request failed; the caller tries again later
def load_or_make(conn, home: Location, uni: Location, api_key: str) -> list[WeatherPoint]:
    if points := store.get_points(conn, home.id, uni.id):
        return points

    try:
        database.record_request(conn, "tomtom_routing")
        line = fetch_route_line(home, uni, api_key)

    # The TomTom key is part of the request URL, so redact it from the error text
    except Exception as err:
        logger.error(f"Weather points: route request failed: {str(err).replace(api_key, '***')}")
        return []

    store.add_points(conn, home.id, uni.id, [(seq, *point) for seq, point in enumerate(spaced(line))])
    points = store.get_points(conn, home.id, uni.id)
    logger.info(f"Weather points: {len(points)} along the {points[-1].route_km:.0f} km toll-free route, every {spacing_km:g} km")
    return points

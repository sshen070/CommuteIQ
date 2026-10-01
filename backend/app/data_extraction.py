from location import Location
from urllib.parse import quote

import requests

routing_url: str = "https://api.tomtom.com/routing/1/calculateRoute/"
search_url: str = "https://api.tomtom.com/search/2/"

# Seconds before giving up on a TomTom request
timeout: int = 10


def fetch_route(origin: Location, destination: Location, api_key: str, allow_tolls: bool = False) -> dict:

    # Create base url
    coords: str = f'{origin.lat},{origin.lon}:{destination.lat},{destination.lon}'
    base_path: str = f"{routing_url}{coords}/json"

    # Create query ~ determine optimal path (leaving now) with/without tollRoads
    params: dict = {
        "key": api_key,
        "traffic": "true",
        "travelMode": "car",
        "routeType": "fastest",
    }

    # Avoid paid roads unless the user is fine with tolls
    if not allow_tolls:
        params["avoid"] = "tollRoads"

    response = requests.get(base_path, params=params, timeout=timeout)
    check_response(response)

    # Route stats: travel time, traffic delay, distance, departure/arrival
    return response.json()["routes"][0]["summary"]


def search_places(query: str, api_key: str, limit: int = 5) -> list[Location]:

    # Fuzzy search matches addresses and POIs (e.g. parking lots), best match first
    params = {"key": api_key, "limit": limit, "countrySet": "US"}

    response = requests.get(f"{search_url}search/{quote(query, safe='')}.json", params=params, timeout=timeout)
    check_response(response)

    return [
        Location(
            address=result["address"].get("freeformAddress", ""),
            lat=result["position"]["lat"],
            lon=result["position"]["lon"],
            name=result.get("poi", {}).get("name"),
        )
        for result in response.json()["results"]
    ]


def reverse_geocode(lat: float, lon: float, api_key: str) -> str | None:

    # Nearest street address to a coordinate
    response = requests.get(f"{search_url}reverseGeocode/{lat},{lon}.json", params={"key": api_key}, timeout=timeout)
    check_response(response)

    addresses = response.json()["addresses"]
    return addresses[0]["address"].get("freeformAddress") if addresses else None


def check_response(response):

    # Report failures without the request URL, which contains the API key
    if response.status_code != 200:
        raise RuntimeError(f"TomTom request failed: HTTP {response.status_code} {response.text[:200]}")

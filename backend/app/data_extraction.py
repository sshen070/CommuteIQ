from location import Location
from urllib.parse import quote

import requests

routing_url: str = "https://api.tomtom.com/routing/1/calculateRoute/"
search_url: str = "https://api.tomtom.com/search/2/"

google_routes_url: str = "https://routes.googleapis.com/directions/v2:computeRoutes"
google_places_url: str = "https://places.googleapis.com/v1/places:searchText"

# Seconds before giving up on a TomTom or Google request
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


def fetch_google_route(origin: Location, destination: Location, api_key: str, allow_tolls: bool = False,
                       traffic_model: str = "PESSIMISTIC") -> dict:

    # Google takes the key in a header, so request URLs never contain it
    headers = {
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": "routes.duration,routes.staticDuration,routes.distanceMeters",
    }

    # No departureTime = leaving now. trafficModel needs TRAFFIC_AWARE_OPTIMAL,
    # which bills as Compute Routes Pro (5,000 free per month, then $10 per 1,000)
    body = {
        "origin": google_waypoint(origin),
        "destination": google_waypoint(destination),
        "travelMode": "DRIVE",
        "routingPreference": "TRAFFIC_AWARE_OPTIMAL",
        "trafficModel": traffic_model,
        "routeModifiers": {"avoidTolls": not allow_tolls},
    }

    response = requests.post(google_routes_url, headers=headers, json=body, timeout=timeout)
    check_response(response, "Google Routes")

    # Route stats: duration with/without traffic (strings like "1534s"), distance
    return response.json()["routes"][0]


def search_google_places(query: str, api_key: str, limit: int = 5) -> list[Location]:

    # Text Search finds POIs TomTom doesn't index, e.g. individual UCR parking lots.
    # These fields bill as Text Search Pro (5,000 free per month)
    headers = {
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": "places.displayName,places.formattedAddress,places.location",
    }
    body = {"textQuery": query, "pageSize": limit, "regionCode": "us"}

    response = requests.post(google_places_url, headers=headers, json=body, timeout=timeout)
    check_response(response, "Google Places")

    return [
        Location(
            address=place.get("formattedAddress", ""),
            lat=place["location"]["latitude"],
            lon=place["location"]["longitude"],
            name=place.get("displayName", {}).get("text"),
        )
        for place in response.json().get("places", [])
    ]


def google_waypoint(place: Location) -> dict:
    return {"location": {"latLng": {"latitude": place.lat, "longitude": place.lon}}}


def check_response(response, service: str = "TomTom"):

    # Report failures without the request URL, which contains the TomTom key.
    # Error bodies are often pretty-printed JSON, so keep them to one log line
    if response.status_code != 200:
        body = " ".join(response.text.split())[:200]
        raise RuntimeError(f"{service} request failed: HTTP {response.status_code} {body}")

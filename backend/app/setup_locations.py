from data_extraction import reverse_geocode, search_places
from dotenv import load_dotenv
from location import Location

import config
import database
import os
import re

# Keys must match the role CHECK in database.py and the lookups in main.py.
# Each can be answered in .env as {ROLE}_LOCATION and {ROLE}_NAME.
prompts = {
    "HOME": "Home",
    "UNI": "University (where you park / arrive)",
}
# Coordinates typed directly, e.g. "33.9695, -117.3327" (a pin copied from a map)
coords_pattern = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$")


def find_candidates(query: str, api_key: str) -> list[Location]:
    match = coords_pattern.match(query)

    if match:
        lat, lon = float(match[1]), float(match[2])
        address = reverse_geocode(lat, lon, api_key) or query
        return [Location(address=address, lat=lat, lon=lon)]

    return search_places(query, api_key)


def pick(candidates: list[Location]) -> Location | None:

    # Always confirm: results with near-identical scores can be miles apart
    for i, place in enumerate(candidates, start=1):
        print(f"  {i}. {place}  ({place.lat:.5f}, {place.lon:.5f})")
    print("  0. None of these - search again")

    while True:
        choice = input(f"Choose 0-{len(candidates)} [1]: ").strip() or "1"
        if choice.isdigit() and int(choice) <= len(candidates):
            break
        print("  Invalid choice.")

    return candidates[int(choice) - 1] if choice != "0" else None


def choose_place(prompt: str, api_key: str, query: str | None = None) -> Location:

    while True:
        # A query from .env is tried first; after that, ask
        if not query:
            query = input(f"{prompt} - address, place name, or 'lat,lon': ").strip()
            if not query:
                continue

        candidates = find_candidates(query, api_key)
        query = None

        if not candidates:
            print("  No matches. Try a fuller address, or paste coordinates as 'lat,lon'.")
            continue

        place = pick(candidates)
        if place is not None:
            return place


def setup_place(conn, role: str, prompt: str, api_key: str):
    current = database.get_location(conn, role)
    env_query = config.env(f"{role}_LOCATION")
    env_name = config.env(f"{role}_NAME")

    if env_query:
        # Coordinates from .env are exact, so use them as-is; an address still needs confirming
        if coords_pattern.match(env_query):
            place = find_candidates(env_query, api_key)[0]
        else:
            print(f"{prompt}: searching for {role}_LOCATION from .env")
            place = choose_place(prompt, api_key, env_query)

        # Re-running setup with the same .env shouldn't add duplicate rows
        same_spot = current and (current.lat, current.lon) == (place.lat, place.lon)
        if same_spot and env_name in (None, current.name):
            print(f"{prompt}: {current} (unchanged)")
            return

    else:
        if current:
            keep = input(f"{prompt} is set to: {current}\nKeep it? [Y/n]: ").strip().lower()
            if keep in ("", "y", "yes"):
                return

        place = choose_place(prompt, api_key)

    # Label for queries and logs; addresses and 'lat,lon' picks have no POI name
    if env_name:
        place.name = env_name
    else:
        default = f" [{place.name}]" if place.name else ""
        label = input(f"Name this place, e.g. 'UCR Lot 26' (optional){default}: ").strip()
        if label:
            place.name = label

    # New rows keep old samples tied to the old address (e.g. after moving)
    location = database.add_location(conn, role, place)
    print(f"  Saved {role}: {location}\n")


def setup_tolls(conn):

    # TOLL in .env wins, so there's nothing to ask
    if (env_toll := config.env("TOLL")) is not None:
        print(f"Toll routes: {'on' if config.parse_bool(env_toll) else 'off'} (TOLL in .env)")
        return

    saved = database.get_setting(conn, "allow_tolls") == "1"
    answer = input(
        "Also sample the fastest route with toll roads allowed? "
        f"Uses 4 requests per slot instead of 2 [{'Y/n' if saved else 'y/N'}]: "
    ).strip().lower()
    allow = saved if not answer else answer in ("y", "yes")

    database.set_setting(conn, "allow_tolls", "1" if allow else "0")
    print(f"  Saved toll routes: {'on' if allow else 'off'}")


def main():
    load_dotenv()

    api_key = os.getenv("TOMTOM_API_KEY")
    conn = database.connect()

    for role, prompt in prompts.items():
        setup_place(conn, role, prompt, api_key)

    setup_tolls(conn)


if __name__ == "__main__":
    main()

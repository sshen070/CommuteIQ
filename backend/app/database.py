from dataclasses import replace
from datetime import datetime
from google_route import GoogleRoute
from location import Location
from pathlib import Path
from trip_route import TripRoute

import os
import sqlite3

# CommuteIQ/data/commute.db unless DB_PATH is set in .env
default_db_path: Path = Path(__file__).resolve().parents[2] / "data" / "commute.db"


def connect() -> sqlite3.Connection:
    path = Path(os.getenv("DB_PATH") or default_db_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")

    # If does not already exist
    create_tables(conn)
    return conn


# Create tables if they do not already exist
def create_tables(conn):
    conn.executescript("""
    -- Places resolved via Google or TomTom search; the newest row per role is the active one
    CREATE TABLE IF NOT EXISTS locations (
        id INTEGER PRIMARY KEY,

        role TEXT NOT NULL CHECK (role IN ('HOME', 'UNI')),
        name TEXT,                  -- user's label, or the search result's place name
        address TEXT NOT NULL,
        lat REAL NOT NULL,
        lon REAL NOT NULL,

        -- Local ISO-8601 with UTC offset, set by add_location
        created_at TEXT NOT NULL
    );

    -- Answers saved by setup, e.g. allow_tolls = '1'; a value in .env wins over these
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );

    -- One row per route query, in raw TomTom units (seconds, meters).
    -- Timestamps are local ISO-8601 with UTC offset, e.g. 2026-09-29T07:35:00-07:00
    CREATE TABLE IF NOT EXISTS commute_samples (
        id INTEGER PRIMARY KEY,

        origin_id INTEGER NOT NULL REFERENCES locations(id),
        destination_id INTEGER NOT NULL REFERENCES locations(id),

        -- 1 = toll roads permitted on this route, 0 = toll roads avoided
        allow_tolls INTEGER NOT NULL CHECK (allow_tolls IN (0, 1)),

        departure_at TEXT NOT NULL,
        arrival_at TEXT NOT NULL,

        -- Local departure split out for easy filtering, e.g. 2026-09-29 / Tuesday / 07:35
        departure_date TEXT NOT NULL,
        departure_weekday TEXT NOT NULL,
        departure_time TEXT NOT NULL,

        travel_time_s INTEGER NOT NULL,
        traffic_delay_s INTEGER NOT NULL,
        length_m INTEGER NOT NULL,
        traffic_length_m INTEGER NOT NULL
    );

    -- One row per Google Routes query, only in weekday commute windows (seconds, meters).
    -- Column names match commute_samples where the meaning does, for side-by-side queries.
    -- Google returns no timestamps, so departure_at is when the request was sent
    CREATE TABLE IF NOT EXISTS google_samples (
        id INTEGER PRIMARY KEY,

        origin_id INTEGER NOT NULL REFERENCES locations(id),
        destination_id INTEGER NOT NULL REFERENCES locations(id),

        -- 1 = toll roads permitted on this route, 0 = toll roads avoided
        allow_tolls INTEGER NOT NULL CHECK (allow_tolls IN (0, 1)),

        -- BEST_GUESS = Google's typical estimate. PESSIMISTIC (longer than actual on most
        -- days) is no longer sampled, but older rows may have it
        traffic_model TEXT NOT NULL,

        departure_at TEXT NOT NULL,
        departure_date TEXT NOT NULL,
        departure_weekday TEXT NOT NULL,
        departure_time TEXT NOT NULL,

        travel_time_s INTEGER NOT NULL,         -- with traffic, under traffic_model
        no_traffic_time_s INTEGER NOT NULL,     -- Google's staticDuration
        length_m INTEGER NOT NULL
    );

    -- API requests sent per local calendar month, counted before sending, so
    -- use_request can refuse Google requests past the monthly limit
    CREATE TABLE IF NOT EXISTS api_usage (
        api TEXT NOT NULL,          -- 'tomtom_routing', 'tomtom_search', 'tomtom_reverse_geocode', 'google_routes', 'google_places'
        month TEXT NOT NULL,        -- e.g. 2026-10
        requests INTEGER NOT NULL,
        PRIMARY KEY (api, month)
    );
    """)


def add_location(conn, role: str, place: Location) -> Location:

    # Local time like commute_samples; SQLite's CURRENT_TIMESTAMP is always UTC
    created_at = datetime.now().astimezone().isoformat(timespec="seconds")

    cur = conn.execute(
        "INSERT INTO locations (role, name, address, lat, lon, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (role, place.name, place.address, place.lat, place.lon, created_at)
    )
    conn.commit()

    return replace(place, role=role, id=cur.lastrowid)


# Latest saved location for a role, or None if setup has not been run
def get_location(conn, role: str) -> Location | None:
    row = conn.execute(
        """
        SELECT address, lat, lon, name, role, id
        FROM locations
        WHERE role = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (role,)
    ).fetchone()

    return Location(*row) if row else None


def get_setting(conn, key: str) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_setting(conn, key: str, value: str):
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (key, value)
    )
    conn.commit()


def count_samples(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM commute_samples").fetchone()[0]


def requests_this_month(conn, api: str) -> int:
    row = conn.execute(
        "SELECT requests FROM api_usage WHERE api = ? AND month = ?",
        (api, datetime.now().strftime("%Y-%m"))
    ).fetchone()

    return row[0] if row else 0


# Requests per month for one API, e.g. {"2026-10": 14520}
def requests_by_month(conn, api: str) -> dict[str, int]:
    return dict(conn.execute("SELECT month, requests FROM api_usage WHERE api = ?", (api,)).fetchall())


# Counts one request this month and returns the new total. Called before sending,
# so a request that fails after reaching the API still counts
def record_request(conn, api: str) -> int:
    row = conn.execute(
        """
        INSERT INTO api_usage (api, month, requests) VALUES (?, ?, 1)
        ON CONFLICT (api, month) DO UPDATE SET requests = requests + 1
        RETURNING requests
        """,
        (api, datetime.now().strftime("%Y-%m"))
    ).fetchone()
    conn.commit()

    return row[0]


# Like record_request, but returns None without counting once the limit is reached,
# in which case the caller must not send the request
def use_request(conn, api: str, monthly_limit: int) -> int | None:
    if requests_this_month(conn, api) >= monthly_limit:
        return None

    return record_request(conn, api)


# TomTom requests weren't counted before api_usage tracked them, so seed this month's
# routing count from the stored samples (each one is a successful request). Only
# inserts when the month has no count yet, so it never overwrites real counting
def backfill_tomtom_usage(conn):
    month = datetime.now().strftime("%Y-%m")

    conn.execute(
        """
        INSERT OR IGNORE INTO api_usage (api, month, requests)
        SELECT 'tomtom_routing', ?, COUNT(*) FROM commute_samples WHERE substr(departure_date, 1, 7) = ?
        """,
        (month, month)
    )
    conn.commit()


def add_sample(conn, origin: Location, destination: Location, route: TripRoute, allow_tolls: bool):
    conn.execute(
        """
        INSERT INTO commute_samples
        (
            origin_id,
            destination_id,
            allow_tolls,
            departure_at,
            arrival_at,
            departure_date,
            departure_weekday,
            departure_time,
            travel_time_s,
            traffic_delay_s,
            length_m,
            traffic_length_m
        )
        VALUES
        (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            origin.id,
            destination.id,
            int(allow_tolls),
            route.departureTime,
            route.arrivalTime,
            route.departure_date,
            route.departure_weekday,
            route.departure_time,
            route.travelTimeInSeconds,
            route.trafficDelayInSeconds,
            route.lengthInMeters,
            route.trafficLengthInMeters
        )
    )
    conn.commit()


def add_google_sample(conn, origin: Location, destination: Location, route: GoogleRoute, allow_tolls: bool):
    conn.execute(
        """
        INSERT INTO google_samples
        (
            origin_id,
            destination_id,
            allow_tolls,
            traffic_model,
            departure_at,
            departure_date,
            departure_weekday,
            departure_time,
            travel_time_s,
            no_traffic_time_s,
            length_m
        )
        VALUES
        (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            origin.id,
            destination.id,
            int(allow_tolls),
            route.trafficModel,
            route.departureTime,
            route.departure_date,
            route.departure_weekday,
            route.departure_time,
            route.travel_time_s,
            route.no_traffic_time_s,
            route.distanceMeters
        )
    )
    conn.commit()

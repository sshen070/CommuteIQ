from dataclasses import replace
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
    -- Places resolved via TomTom search; the newest row per role is the active one
    CREATE TABLE IF NOT EXISTS locations (
        id INTEGER PRIMARY KEY,

        role TEXT NOT NULL CHECK (role IN ('HOME', 'UNI')),
        name TEXT,
        address TEXT NOT NULL,
        lat REAL NOT NULL,
        lon REAL NOT NULL,

        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
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
    """)


def add_location(conn, role: str, place: Location) -> Location:
    cur = conn.execute(
        "INSERT INTO locations (role, name, address, lat, lon) VALUES (?, ?, ?, ?, ?)",
        (role, place.name, place.address, place.lat, place.lon)
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

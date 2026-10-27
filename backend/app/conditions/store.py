from dataclasses import dataclass
from datetime import date, datetime

# Tables and queries for weather. CREATE TABLE IF NOT EXISTS adds them to an existing
# database without touching the drive-time tables.


@dataclass
class WeatherPoint:
    id: int
    seq: int
    route_km: float
    lat: float
    lon: float


def create_tables(conn):
    conn.executescript("""
    -- Fixed points along the toll-free route from home to uni where weather is read, made
    -- once per home/uni pair from TomTom's route line. Coordinates are rounded to 0.01°
    -- (about 1 km); weather models use grids of 3 km and up
    CREATE TABLE IF NOT EXISTS weather_points (
        id INTEGER PRIMARY KEY,

        home_id INTEGER NOT NULL REFERENCES locations(id),
        uni_id INTEGER NOT NULL REFERENCES locations(id),

        seq INTEGER NOT NULL,           -- 0 at home, increasing toward uni
        route_km REAL NOT NULL,         -- distance from home along the route
        lat REAL NOT NULL,
        lon REAL NOT NULL,

        created_at TEXT NOT NULL,       -- local ISO-8601 with UTC offset

        UNIQUE (home_id, uni_id, seq)
    );

    -- Open-Meteo weather per point and 15-minute block, in its raw units. source 'live' was
    -- read by the collector as it happened; 'archive' was filled in later from Open-Meteo's
    -- historical forecast archive (python -m conditions.backfill_weather). Both come from
    -- the same 15-minute model data (HRRR in the US)
    CREATE TABLE IF NOT EXISTS weather_samples (
        id INTEGER PRIMARY KEY,

        point_id INTEGER NOT NULL REFERENCES weather_points(id),
        source TEXT NOT NULL CHECK (source IN ('live', 'archive')),

        -- End of the 15-minute block, local ISO-8601 with UTC offset. Precipitation is the
        -- total over the 15 minutes before it; the rest are values at that time
        observed_at TEXT NOT NULL,
        fetched_at TEXT NOT NULL,

        precip_mm REAL,
        weather_code INTEGER,           -- WMO: 45/48 fog, 51-57 drizzle, 61-67 rain, 80-82 showers, 95+ thunder
        visibility_m REAL,
        temp_c REAL,
        cloud_cover_pct REAL,
        wind_kmh REAL,
        gust_kmh REAL,
        wind_dir_deg REAL,              -- where the wind blows from; Santa Anas come from the northeast

        UNIQUE (point_id, source, observed_at)
    );

    -- The latest Open-Meteo forecast per point and 15-minute block, from the start of today
    -- through tomorrow evening. Each refresh overwrites the blocks with the newer forecast
    -- and drops the days before today, so this holds only what's still ahead or today's;
    -- weather_samples keeps what actually happened. Same columns and units
    CREATE TABLE IF NOT EXISTS weather_forecasts (
        point_id INTEGER NOT NULL REFERENCES weather_points(id),
        valid_at TEXT NOT NULL,         -- end of the block, local ISO-8601 like observed_at
        fetched_at TEXT NOT NULL,       -- when this forecast was fetched

        precip_mm REAL,
        weather_code INTEGER,
        visibility_m REAL,
        temp_c REAL,
        cloud_cover_pct REAL,
        wind_kmh REAL,
        gust_kmh REAL,
        wind_dir_deg REAL,
        precip_prob_pct REAL,           -- chance of 0.1 mm or more in the block's hour

        PRIMARY KEY (point_id, valid_at)
    );
    """)


# Points for a home/uni pair, ordered from home to uni
def get_points(conn, home_id: int, uni_id: int) -> list[WeatherPoint]:
    rows = conn.execute(
        "SELECT id, seq, route_km, lat, lon FROM weather_points WHERE home_id = ? AND uni_id = ? ORDER BY seq",
        (home_id, uni_id)
    ).fetchall()

    return [WeatherPoint(*row) for row in rows]


# points: (seq, route_km, lat, lon) tuples
def add_points(conn, home_id: int, uni_id: int, points: list[tuple]):
    created_at = datetime.now().astimezone().isoformat(timespec="seconds")

    conn.executemany(
        "INSERT INTO weather_points (home_id, uni_id, seq, route_km, lat, lon, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [(home_id, uni_id, *point, created_at) for point in points]
    )
    conn.commit()


weather_columns = [
    "precip_mm", "weather_code", "visibility_m", "temp_c",
    "cloud_cover_pct", "wind_kmh", "gust_kmh", "wind_dir_deg",
]


# rows: (point_id, observed_at, fetched_at, *weather_columns). A block read again replaces
# the earlier reading, since a newer model run may have updated it
def add_weather(conn, rows: list[tuple], source: str):
    columns = ", ".join(weather_columns)
    updates = ", ".join(f"{column} = excluded.{column}" for column in ["fetched_at", *weather_columns])

    conn.executemany(
        f"""
        INSERT INTO weather_samples (point_id, source, observed_at, fetched_at, {columns})
        VALUES (?, ?, ?, ?, {", ".join("?" * len(weather_columns))})
        ON CONFLICT (point_id, source, observed_at) DO UPDATE SET {updates}
        """,
        [(row[0], source, *row[1:]) for row in rows]
    )
    conn.commit()


# rows: (point_id, valid_at, fetched_at, *weather_columns, precip_prob_pct). A block fetched
# again is overwritten, so each block keeps the newest forecast
def add_forecast(conn, rows: list[tuple]):
    columns = [*weather_columns, "precip_prob_pct"]
    updates = ", ".join(f"{column} = excluded.{column}" for column in ["fetched_at", *columns])

    conn.executemany(
        f"""
        INSERT INTO weather_forecasts (point_id, valid_at, fetched_at, {", ".join(columns)})
        VALUES (?, ?, ?, {", ".join("?" * len(columns))})
        ON CONFLICT (point_id, valid_at) DO UPDATE SET {updates}
        """,
        rows
    )
    conn.commit()


# Forecast blocks from local days before today, which weather_samples now covers. Returns how many
def drop_past_forecasts(conn, today: date) -> int:
    count = conn.execute("DELETE FROM weather_forecasts WHERE substr(valid_at, 1, 10) < ?", (today.isoformat(),)).rowcount
    conn.commit()
    return count

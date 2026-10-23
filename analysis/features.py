from contextlib import closing
from datetime import date
from dotenv import load_dotenv
from pathlib import Path

import calendar_days
import numpy as np
import os
import pandas as pd
import samples
import sqlite3
import sys

# Factors besides traffic for each TomTom sample: the weather the drive went through, the
# sun on each stretch of road, and the calendar. Weather comes from the database, where
# the collector reads it live at points along the route and backfill_weather fills in
# the past. The sun is computed and the calendar is kept by hand (calendar_days.py), so
# neither is stored. Raw units (mm, km, km/h, °C); day_log.py converts for display.

local_tz = "America/Los_Angeles"

# A 15-minute block with wet_block_mm or more at a point counts as wet there. A day with
# wet_day_mm or more at any point ends a dry spell
wet_block_mm: float = 0.1
wet_day_mm: float = 1.0

# Roads stay wet for a while after the rain stops
wet_road_hours: int = 3

# backfill_weather reaches this far back, so dry_days of this many means "at least"
dry_cap_days: int = 120

# Local hours [start, end) summarized per day; covers both commute windows
day_hours: tuple[int, int] = (5, 21)

# Glare: the sun is up, under glare_max_elev degrees, and within glare_max_offset degrees
# of the direction of travel on that stretch of road
glare_max_elev: float = 25
glare_max_offset: float = 30

weather_columns = [
    "precip_mm", "weather_code", "visibility_m", "temp_c", "cloud_cover_pct", "wind_kmh", "gust_kmh", "wind_dir_deg",
]

no_weather = "No weather in the database yet. Run python -m conditions.backfill_weather (see CLAUDE.md)."


# TomTom samples between the active home and uni, the weather points along the route, and
# their weather (one reading per point and 15-minute block, live preferred over archive).
# The points' rounded coordinates stay in memory and are never printed or written
def load() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    load_dotenv(samples.repo_root / ".env")
    path = Path((os.getenv("DB_PATH") or "").strip() or samples.default_db_path).resolve()

    if not path.exists():
        sys.exit(f"No database at {path}. Start the collector first (see CLAUDE.md).")

    # Read-only, and closed as soon as the rows are in memory: an open transaction
    # would make the collector's inserts fail with "database is locked"
    with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as conn:
        active = dict(conn.execute("SELECT role, MAX(id) FROM locations GROUP BY role").fetchall())

        if "HOME" not in active or "UNI" not in active:
            sys.exit("Home/university locations are not set. Run setup_locations.py first.")

        df = pd.read_sql_query(
            """
            SELECT origin_id, destination_id, allow_tolls, departure_at,
                   departure_date, departure_weekday, departure_time, travel_time_s
            FROM commute_samples
            """,
            conn,
        )

        # The weather tables appear once the collector or backfill_weather has run
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'weather_points'").fetchone():
            sys.exit(no_weather)

        pair = (active["HOME"], active["UNI"])
        points = pd.read_sql_query(
            "SELECT id AS point_id, route_km, lat, lon FROM weather_points WHERE home_id = ? AND uni_id = ? ORDER BY seq",
            conn, params=pair,
        )
        weather = pd.read_sql_query(
            f"""
            SELECT point_id, source, observed_at, {", ".join(weather_columns)}
            FROM weather_samples
            WHERE point_id IN (SELECT id FROM weather_points WHERE home_id = ? AND uni_id = ?)
            """,
            conn, params=pair,
        )

    if points.empty or weather.empty:
        sys.exit(no_weather)

    # Samples measured from an earlier address belong to a different route
    role_of = {active["HOME"]: "HOME", active["UNI"]: "UNI"}
    current = df["origin_id"].isin(role_of) & df["destination_id"].isin(role_of)
    if older := int((~current).sum()):
        print(f"Note: skipped {older:,} samples measured from an earlier home/uni address.")

    df = df[current].copy()
    if df.empty:
        sys.exit("No samples yet for the current home/uni. Let the collector run first.")

    df["origin"] = df["origin_id"].map(role_of)
    df["destination"] = df["destination_id"].map(role_of)

    # departure_time is local clock time, so DST changes need no special handling
    clock = df["departure_time"].str.split(":", expand=True).astype(int)
    df["dep_min"] = clock[0] * 60 + clock[1]
    df["slot"] = df["dep_min"] // samples.slot_min * samples.slot_min
    df["travel_min"] = df["travel_time_s"] / 60

    # Offsets change when DST ends, so parse to UTC for weather blocks and the sun
    df["departure_utc"] = pd.to_datetime(df["departure_at"], utc=True)

    # Blocks are labeled by their end. 'live' sorts after 'archive', so keeping the last
    # row of each block keeps the reading taken at the time
    weather["time"] = pd.to_datetime(weather["observed_at"], utc=True)
    weather = weather.sort_values("source").drop_duplicates(["point_id", "time"], keep="last")

    return df.drop(columns=["origin_id", "destination_id", "departure_at", "travel_time_s"]), points, weather


# Initial compass bearing between points, in degrees (works on arrays)
def bearing(lat1, lon1, lat2, lon2):
    lat1, lon1, lat2, lon2 = (np.radians(np.asarray(v, dtype=float)) for v in (lat1, lon1, lat2, lon2))
    y = np.sin(lon2 - lon1) * np.cos(lat2)
    x = np.cos(lat1) * np.sin(lat2) - np.sin(lat1) * np.cos(lat2) * np.cos(lon2 - lon1)
    return np.degrees(np.arctan2(y, x)) % 360


# The stored weather points (placed every 5 km along TomTom's route line by the collector)
# with the stretch of road that starts at each, in both directions: its compass heading,
# which glare needs, and its length. Toward uni a stretch runs to the next point; toward
# home, to the previous one
def stretches(points: pd.DataFrame) -> pd.DataFrame:
    line = points.reset_index(drop=True).copy()
    following, previous = line.shift(-1), line.shift(1)
    line["heading_out"] = bearing(line["lat"], line["lon"], following["lat"], following["lon"])
    line["stretch_out_km"] = (following["route_km"] - line["route_km"]).fillna(0)
    line["heading_back"] = bearing(line["lat"], line["lon"], previous["lat"], previous["lon"])
    line["stretch_back_km"] = (line["route_km"] - previous["route_km"]).fillna(0)
    return line


# One row per sample and route point, with when the car passes the point: departure plus
# the drive time scaled by the share of the route already behind it. Index = sample label
def stops(df: pd.DataFrame, line: pd.DataFrame) -> pd.DataFrame:
    total = line["route_km"].iloc[-1]
    trips = df[["origin", "departure_utc", "travel_min"]].rename_axis("sample").reset_index().merge(line, how="cross")

    outbound = trips["origin"] == "HOME"
    covered = np.where(outbound, trips["route_km"], total - trips["route_km"]) / total
    trips["passed_at"] = trips["departure_utc"] + pd.to_timedelta(trips["travel_min"] * covered, unit="min")
    trips["heading"] = np.where(outbound, trips["heading_out"], trips["heading_back"])
    trips["stretch_km"] = np.where(outbound, trips["stretch_out_km"], trips["stretch_back_km"])
    return trips


# Weather the drive went through: each point's 15-minute block when the car passed it.
# Worst along the way for rain, visibility and gusts; average for temperature and cloud.
# Wind against the car, only on stretches where rain was falling (NaN on a dry drive):
# rain_headwind_kmh is the km-weighted mean headwind (+ into the wind, - tailwind; the wind
# direction is where it blows from, so a wind from the heading is a headwind), and
# rain_cross_gust_kmh the strongest gust across the road
def trip_weather(trips: pd.DataFrame, weather: pd.DataFrame) -> pd.DataFrame:
    # Rain at each point over the last few hours. The rolling result comes back ordered by
    # point and time, the same order as the sorted frame
    weather = weather.sort_values(["point_id", "time"]).copy()
    weather["wet_road_mm"] = (
        weather.groupby("point_id").rolling(f"{wet_road_hours}h", on="time")["precip_mm"].sum().to_numpy()
    )

    passed = trips.assign(time=trips["passed_at"].dt.ceil("15min")).merge(
        weather[["point_id", "time", "wet_road_mm", *weather_columns]], on=["point_id", "time"], how="left"
    )
    passed["wet"] = np.where(passed["precip_mm"].isna(), np.nan, passed["precip_mm"] >= wet_block_mm)

    raining = passed["precip_mm"] >= wet_block_mm
    offset = np.radians(passed["wind_dir_deg"] - passed["heading"])
    headwind = passed["wind_kmh"] * np.cos(offset)
    passed["rain_km"] = passed["stretch_km"].where(raining, 0)
    passed["rain_headwind_x_km"] = (headwind * passed["stretch_km"]).where(raining, 0)
    passed["rain_cross_gust_kmh"] = (passed["gust_kmh"] * np.abs(np.sin(offset))).where(raining)

    out = passed.groupby("sample").agg(
        precip_mm=("precip_mm", "max"),
        wet_share=("wet", "mean"),
        wet_road_mm=("wet_road_mm", "max"),
        weather_code=("weather_code", "max"),
        visibility_km=("visibility_m", "min"),
        gust_kmh=("gust_kmh", "max"),
        temp_c=("temp_c", "mean"),
        cloud_cover_pct=("cloud_cover_pct", "mean"),
        rain_km=("rain_km", "sum"),
        rain_headwind_x_km=("rain_headwind_x_km", "sum"),
        rain_cross_gust_kmh=("rain_cross_gust_kmh", "max"),
    )
    out["visibility_km"] /= 1000
    out["rain_headwind_kmh"] = (out["rain_headwind_x_km"] / out["rain_km"]).where(out["rain_km"] > 0)
    return out.drop(columns=["rain_km", "rain_headwind_x_km"])


# Sun elevation and azimuth in degrees (azimuth clockwise from north) for UTC times, from
# NOAA's low-precision solar equations, good to about half a degree. lat/lon may be arrays
def sun_position(utc, lat, lon) -> tuple[np.ndarray, np.ndarray]:
    utc = pd.DatetimeIndex(utc).tz_convert("UTC")
    hours = (utc.hour + utc.minute / 60 + utc.second / 3600).to_numpy()
    g = 2 * np.pi / 365 * (utc.dayofyear.to_numpy() - 1 + (hours - 12) / 24)

    eqtime = 229.18 * (0.000075 + 0.001868 * np.cos(g) - 0.032077 * np.sin(g)
                       - 0.014615 * np.cos(2 * g) - 0.040849 * np.sin(2 * g))
    decl = (0.006918 - 0.399912 * np.cos(g) + 0.070257 * np.sin(g) - 0.006758 * np.cos(2 * g)
            + 0.000907 * np.sin(2 * g) - 0.002697 * np.cos(3 * g) + 0.00148 * np.sin(3 * g))

    hour_angle = np.radians((hours * 60 + eqtime + 4 * np.asarray(lon)) / 4 - 180)
    phi = np.radians(np.asarray(lat))

    cos_zenith = np.sin(phi) * np.sin(decl) + np.cos(phi) * np.cos(decl) * np.cos(hour_angle)
    elevation = 90 - np.degrees(np.arccos(np.clip(cos_zenith, -1, 1)))
    azimuth = np.degrees(np.arctan2(np.sin(hour_angle), np.cos(hour_angle) * np.sin(phi) - np.tan(decl) * np.cos(phi)))

    return elevation, (azimuth + 180) % 360


# Sun on each stretch as the car starts it: km driven into a low sun (glare) and km in the
# dark, plus the sun's elevation at departure
def trip_sun(trips: pd.DataFrame) -> pd.DataFrame:
    elevation, azimuth = sun_position(trips["passed_at"], trips["lat"].to_numpy(), trips["lon"].to_numpy())
    offset = (azimuth - trips["heading"].to_numpy() + 180) % 360 - 180
    glare = (elevation > 0) & (elevation < glare_max_elev) & (np.abs(offset) <= glare_max_offset)

    sun = trips[["sample", "stretch_km", "passed_at"]].assign(
        glare_km=trips["stretch_km"] * glare, dark_km=trips["stretch_km"] * (elevation < 0), elevation=elevation,
    )
    out = sun.groupby("sample").agg(glare_km=("glare_km", "sum"), dark_km=("dark_km", "sum"))
    out["sun_elev"] = sun.loc[sun.groupby("sample")["passed_at"].idxmin()].set_index("sample")["elevation"]
    return out


# Per local date: weather during commute hours, and how long it had been dry
def daily_weather(weather: pd.DataFrame) -> pd.DataFrame:
    columns = ["rain_mm", "wet_hours", "min_visibility_km", "max_gust_kmh", "high_c", "dry_days"]
    local = weather["time"].dt.tz_convert(local_tz)
    weather = weather.assign(date=local.dt.strftime("%Y-%m-%d"))
    commute = weather[(local.dt.hour >= day_hours[0]) & (local.dt.hour < day_hours[1])]
    by_day = commute.groupby("date")

    days = pd.DataFrame({
        # Rain at the wettest point, and hours (in 15-minute blocks) with rain anywhere
        "rain_mm": commute.groupby(["date", "point_id"])["precip_mm"].sum().groupby(level="date").max(),
        "wet_hours": commute[commute["precip_mm"] >= wet_block_mm].groupby("date")["time"].nunique() / 4,
        "min_visibility_km": by_day["visibility_m"].min() / 1000,
        "max_gust_kmh": by_day["gust_kmh"].max(),
        "high_c": by_day["temp_c"].max(),
    })
    days["wet_hours"] = days["wet_hours"].fillna(0)

    # Whole-day rain at the wettest point decides whether a day ends a dry spell
    totals = weather.groupby(["date", "point_id"])["precip_mm"].sum().groupby(level="date").max()
    days["dry_days"] = dry_days(totals)
    return days[columns]


# Days since the last wet day, not counting the day itself, so the first rain after a
# dry spell shows how long the spell was. Capped at dry_cap_days
def dry_days(day_totals: pd.Series) -> pd.Series:
    dates = pd.Series(pd.to_datetime(day_totals.index), index=day_totals.index)
    last_wet = dates.where(day_totals >= wet_day_mm).ffill().shift(1)
    return (dates - last_wet).dt.days.fillna(dry_cap_days).clip(upper=dry_cap_days).astype(int)


# calendar_days.tags for each local date (YYYY-MM-DD strings)
def calendar(dates) -> pd.DataFrame:
    try:
        entries = calendar_days.load()
    except (OSError, ValueError) as err:
        sys.exit(str(err))

    dates = sorted(dates)
    rows = [calendar_days.tags(date.fromisoformat(day), entries) for day in dates]
    return pd.DataFrame(rows, index=pd.Index(dates, name="date"))


# Every sample with its features, and calendar and weather per collected day
def build(df: pd.DataFrame, points: pd.DataFrame, weather: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    trips = stops(df, stretches(points))
    df = df.join(trip_weather(trips, weather)).join(trip_sun(trips))

    days = calendar(df["departure_date"].unique()).join(daily_weather(weather))

    per_day = ["dry_days", "holiday", "days_to_holiday", "uni", "k12_break", "notes", "dst_days"]
    df = df.merge(days[per_day], left_on="departure_date", right_index=True, how="left")
    return df, days


# Local departures (minutes after midnight) on a date that would drive into a low sun, with
# the km of glare. typical_min maps each departure slot to its usual drive time, which sets
# when each stretch is reached
def glare_departures(day: date, origin: str, line: pd.DataFrame, typical_min: pd.Series) -> dict[int, float]:
    departures = pd.date_range(f"{day} 00:00", f"{day} 23:55", freq="5min", tz=local_tz)
    minutes = departures.hour * 60 + departures.minute
    travel = (minutes // samples.slot_min * samples.slot_min).map(typical_min).fillna(typical_min.median())

    planned = pd.DataFrame({"origin": origin, "departure_utc": departures.tz_convert("UTC"), "travel_min": travel.to_numpy()})
    sun = trip_sun(stops(planned, line))
    return {int(minutes[i]): km for i, km in sun["glare_km"].items() if km > 0}

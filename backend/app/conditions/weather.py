from conditions import route_points, store
from conditions.store import WeatherPoint
from data_extraction import check_response
from datetime import date, datetime, timedelta, timezone
from location import Location

import config
import database
import logging
import requests
import sys

# Open-Meteo: free for non-commercial use, no key, data under CC BY 4.0. Live readings
# ("current") and the archive both come from 15-minute model data (HRRR in the US), so a
# place's weather changes every 15 minutes; asking more often returns the same block.

forecast_url: str = "https://api.open-meteo.com/v1/forecast"
archive_url: str = "https://historical-forecast-api.open-meteo.com/v1/forecast"

# Open-Meteo variable -> weather_samples column, in Open-Meteo's default units (mm, m, °C,
# %, km/h, degrees), stored as is. Up to 10 variables bill as one call per place
variables = {
    "precipitation": "precip_mm",
    "weather_code": "weather_code",
    "visibility": "visibility_m",
    "temperature_2m": "temp_c",
    "cloud_cover": "cloud_cover_pct",
    "wind_speed_10m": "wind_kmh",
    "wind_gusts_10m": "gust_kmh",
    "wind_direction_10m": "wind_dir_deg",
}

# Seconds before giving up on an Open-Meteo request (archive requests are large)
timeout: int = 30

# Free tier: 10,000 calls a day. How a request for several places bills isn't documented,
# so each place counts as one call here
daily_limit: int = 10_000

# Minutes between live readings unless WEATHER_EVERY_MIN in .env says otherwise:
# the data's own 15-minute step
default_every_min: int = 15

# Readings are taken this many minutes after the quarter hour. On the hour and half hour,
# Open-Meteo often answers 503 "overloaded" (7 of 62 readings on Oct 12, 2026), when
# scheduled jobs everywhere call it. Current conditions are the latest 15-minute block,
# so a reading at :05 gets the same block as one at :00
reading_offset_min: int = 5

logger = logging.getLogger("commuteiq")


# WEATHER_EVERY_MIN from .env: a multiple of the collector's slot that divides an hour,
# so readings land on the same clock times every hour, or None for "off"
def every_min(interval_min: int) -> int | None:
    value = config.env("WEATHER_EVERY_MIN") or str(default_every_min)
    if value.lower() == "off":
        return None

    if not value.isdigit() or int(value) == 0 or int(value) % interval_min or 60 % int(value):
        sys.exit(f"WEATHER_EVERY_MIN must be 5, 10, 15, 20, 30 or 60 minutes, or off (got {value!r})")

    return int(value)


# Error text safe to log: request errors quote the URL, which holds the points' coordinates
def describe_error(err: Exception) -> str:
    return type(err).__name__ if isinstance(err, requests.RequestException) else str(err)


# One request for every point; returns one result per point, in order
def request(url: str, points: list[WeatherPoint], **params) -> list[dict]:
    params = {
        "latitude": ",".join(f"{point.lat:.{route_points.coordinate_digits}f}" for point in points),
        "longitude": ",".join(f"{point.lon:.{route_points.coordinate_digits}f}" for point in points),
        "timezone": "GMT",
        **params,
    }

    response = requests.get(url, params=params, timeout=timeout)
    check_response(response, "Open-Meteo")

    # One place comes back as an object, several as a list
    data = response.json()
    return data if isinstance(data, list) else [data]


# "2026-10-10T22:30" in UTC -> local ISO-8601 with offset, like every other table
def local_time(gmt: str) -> str:
    return datetime.fromisoformat(gmt).replace(tzinfo=timezone.utc).astimezone().isoformat(timespec="seconds")


# A weather_samples row (without source) from Open-Meteo's values for one block
def row(point: WeatherPoint, observed_at: str, fetched_at: str, values: dict) -> tuple:
    named = {column: values.get(variable) for variable, column in variables.items()}
    return (point.id, observed_at, fetched_at, *(named[column] for column in store.weather_columns))


# Current conditions at every point: the latest 15-minute block
def fetch_current(points: list[WeatherPoint]) -> list[tuple]:
    fetched_at = datetime.now().astimezone().isoformat(timespec="seconds")
    places = request(forecast_url, points, current=",".join(variables))

    return [row(point, local_time(place["current"]["time"]), fetched_at, place["current"]) for point, place in zip(points, places)]


# Archived 15-minute blocks at every point for UTC dates start..end, up to now (later
# blocks of today would be forecasts)
def fetch_archive(points: list[WeatherPoint], start: date, end: date) -> list[tuple]:
    fetched_at = datetime.now().astimezone().isoformat(timespec="seconds")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M")
    places = request(archive_url, points, start_date=start.isoformat(), end_date=end.isoformat(), minutely_15=",".join(variables))

    rows = []
    for point, place in zip(points, places):
        blocks = place["minutely_15"]
        for i, stamp in enumerate(blocks["time"]):
            if stamp <= now:
                rows.append(row(point, local_time(stamp), fetched_at, {variable: blocks[variable][i] for variable in variables}))

    return rows


# Live weather at the route's points, read every WEATHER_EVERY_MIN from reading_offset_min
# past the hour, and yesterday's archive at the first reading after midnight, so readings
# that failed leave no gap. main.py calls on_slot each slot; a failure is logged and never
# stops drive-time sampling
class WeatherCollector:

    def __init__(self, conn, home: Location, uni: Location, tomtom_key: str, interval_min: int):
        self.conn, self.home, self.uni, self.tomtom_key = conn, home, uni, tomtom_key
        self.every_min = every_min(interval_min)

        store.create_tables(conn)
        self.points = route_points.load_or_make(conn, home, uni, tomtom_key) if self.every_min else []

    def log_plan(self):
        if not self.every_min:
            logger.info("Weather: off (WEATHER_EVERY_MIN=off)")
            return

        if not self.points:
            logger.warning("Weather: no points along the route yet; trying again at the next reading")
            return

        # Readings, plus one archive request a day
        per_day = len(self.points) * (24 * 60 // self.every_min + 1)
        used = database.requests_this_month(self.conn, "open_meteo")
        logger.info(f"Weather: {len(self.points)} points along the route every {self.every_min} min from "
                    f":{reading_offset_min:02}, and yesterday's archive at 00:{reading_offset_min:02}; "
                    f"~{per_day:,} Open-Meteo calls a day of {daily_limit:,} free ({used:,} this month)")

        # Leave room for backfill_weather and manual checks on the same allowance
        if per_day > daily_limit * 0.8:
            logger.warning("Weather readings use most of Open-Meteo's daily allowance. Raise WEATHER_EVERY_MIN in .env")

    def on_slot(self, slot: datetime):
        if not self.every_min or (slot.minute - reading_offset_min) % self.every_min:
            return

        # Route request failed earlier (e.g. no network at boot): try again
        if not self.points:
            self.points = route_points.load_or_make(self.conn, self.home, self.uni, self.tomtom_key)
            if not self.points:
                return

        self.read_current()

        if slot.hour == 0 and slot.minute == reading_offset_min:
            self.archive_day(slot.date() - timedelta(days=1))

    def read_current(self):
        try:
            database.record_request(self.conn, "open_meteo", len(self.points))
            rows = fetch_current(self.points)
            store.add_weather(self.conn, rows, "live")

            values = {column: [r[3 + i] for r in rows if r[3 + i] is not None] for i, column in enumerate(store.weather_columns)}
            logger.info(
                f"Weather {rows[0][1][11:16]}: rain up to {max(values['precip_mm'], default=0):.1f} mm, "
                f"visibility down to {min(values['visibility_m'], default=0) / 1000:.1f} km, "
                f"gusts up to {max(values['gust_kmh'], default=0):.0f} km/h ({len(rows)} points)"
            )

        except Exception as err:
            logger.error(f"Weather failed: {describe_error(err)}")

    # A local day's blocks from the archive. A live reading of the same block is kept too,
    # and the analysis prefers it
    def archive_day(self, day: date):
        try:
            database.record_request(self.conn, "open_meteo", len(self.points))
            rows = fetch_archive(self.points, day, day + timedelta(days=1))
            store.add_weather(self.conn, rows, "archive")
            logger.info(f"Weather archive for {day:%a %Y-%m-%d}: {len(rows):,} blocks")

        except Exception as err:
            logger.error(f"Weather archive for {day:%a %Y-%m-%d} failed: {describe_error(err)}")

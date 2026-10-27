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

# Minutes between forecast refreshes unless FORECAST_EVERY_MIN says otherwise. The US model
# (HRRR) runs every hour, so an hourly refresh picks up each new run
default_forecast_every_min: int = 60

# Days of forecast per refresh. Times come back in GMT, where a local evening falls on the
# next day, so 3 GMT days are needed to reach tomorrow's evening commute
forecast_days: int = 3

# Readings are taken this many minutes after the quarter hour. On the hour and half hour,
# Open-Meteo often answers 503 "overloaded" (7 of 62 readings on Oct 12, 2026), when
# scheduled jobs everywhere call it. Current conditions are the latest 15-minute block,
# so a reading at :05 gets the same block as one at :00
reading_offset_min: int = 5

logger = logging.getLogger("commuteiq")


# WEATHER_EVERY_MIN (or FORECAST_EVERY_MIN) from .env: a multiple of the collector's slot
# that divides an hour, so readings land on the same clock times every hour, or None for "off"
def every_min(interval_min: int, name: str = "WEATHER_EVERY_MIN", default: int = default_every_min) -> int | None:
    value = config.env(name) or str(default)
    if value.lower() == "off":
        return None

    if not value.isdigit() or int(value) == 0 or int(value) % interval_min or 60 % int(value):
        sys.exit(f"{name} must be 5, 10, 15, 20, 30 or 60 minutes, or off (got {value!r})")

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


# The forecast at every point per 15-minute block, from the start of today (GMT) through
# forecast_days, each block with the chance of rain in its hour. Open-Meteo labels an hourly
# chance by the hour's end, and a block by its own end, so a block takes its end's hour,
# rounded up
def fetch_forecast(points: list[WeatherPoint]) -> list[tuple]:
    fetched_at = datetime.now().astimezone().isoformat(timespec="seconds")
    places = request(forecast_url, points, minutely_15=",".join(variables), hourly="precipitation_probability",
                     forecast_days=forecast_days)

    rows = []
    for point, place in zip(points, places):
        blocks, hours = place["minutely_15"], place["hourly"]
        chance = dict(zip(hours["time"], hours["precipitation_probability"]))
        for i, stamp in enumerate(blocks["time"]):
            end = datetime.fromisoformat(stamp)
            hour = end if end.minute == 0 else end.replace(minute=0) + timedelta(hours=1)
            values = {variable: blocks[variable][i] for variable in variables}
            rows.append((*row(point, local_time(stamp), fetched_at, values), chance.get(hour.strftime("%Y-%m-%dT%H:%M"))))

    return rows


# Fetch the forecast, overwrite the stored one, and drop days before today. Raises on failure.
# Returns the rows fetched
def update_forecast(conn, points: list[WeatherPoint], today: date) -> list[tuple]:
    database.record_request(conn, "open_meteo", len(points))
    rows = fetch_forecast(points)
    store.add_forecast(conn, rows)
    store.drop_past_forecasts(conn, today)
    return rows


# "next 36 h: rain chance up to 40%, gusts up to 30 km/h" from forecast rows
def describe_forecast(rows: list[tuple], hours: int = 36) -> str:
    now = datetime.now().astimezone()
    ahead = [r for r in rows if now <= datetime.fromisoformat(r[1]) <= now + timedelta(hours=hours)]
    gust = 3 + store.weather_columns.index("gust_kmh")
    chance = max((r[-1] for r in ahead if r[-1] is not None), default=0)
    gusts = max((r[gust] for r in ahead if r[gust] is not None), default=0)
    return f"next {hours} h: rain chance up to {chance:.0f}%, gusts up to {gusts:.0f} km/h"


# Live weather at the route's points, read every WEATHER_EVERY_MIN from reading_offset_min
# past the hour, and yesterday's archive at the first reading after midnight, so readings
# that failed leave no gap. The forecast is refreshed every FORECAST_EVERY_MIN on the same
# clock times. main.py calls on_slot each slot; a failure is logged and never stops
# drive-time sampling
class WeatherCollector:

    def __init__(self, conn, home: Location, uni: Location, tomtom_key: str, interval_min: int):
        self.conn, self.home, self.uni, self.tomtom_key = conn, home, uni, tomtom_key
        self.every_min = every_min(interval_min)
        self.forecast_every_min = every_min(interval_min, "FORECAST_EVERY_MIN", default_forecast_every_min)

        store.create_tables(conn)
        needed = self.every_min or self.forecast_every_min
        self.points = route_points.load_or_make(conn, home, uni, tomtom_key) if needed else []

    def log_plan(self):
        if not self.every_min and not self.forecast_every_min:
            logger.info("Weather: off (WEATHER_EVERY_MIN=off, FORECAST_EVERY_MIN=off)")
            return

        if not self.points:
            logger.warning("Weather: no points along the route yet; trying again at the next reading")
            return

        # Readings plus one archive request a day, and forecast refreshes
        readings = len(self.points) * (24 * 60 // self.every_min + 1) if self.every_min else 0
        forecasts = len(self.points) * (24 * 60 // self.forecast_every_min) if self.forecast_every_min else 0
        used = database.requests_this_month(self.conn, "open_meteo")

        if self.every_min:
            logger.info(f"Weather: {len(self.points)} points along the route every {self.every_min} min from "
                        f":{reading_offset_min:02}, and yesterday's archive at 00:{reading_offset_min:02}")
        else:
            logger.info("Weather readings: off (WEATHER_EVERY_MIN=off)")
        if self.forecast_every_min:
            logger.info(f"Forecast: refreshed every {self.forecast_every_min} min from :{reading_offset_min:02},"
                        f" today through tomorrow evening, past days dropped")
        else:
            logger.info("Forecast: off (FORECAST_EVERY_MIN=off)")
        logger.info(f"Open-Meteo: ~{readings + forecasts:,} calls a day of {daily_limit:,} free ({used:,} this month)")

        # Leave room for backfill_weather and manual checks on the same allowance
        if readings + forecasts > daily_limit * 0.8:
            logger.warning("Weather uses most of Open-Meteo's daily allowance. Raise WEATHER_EVERY_MIN or FORECAST_EVERY_MIN in .env")

    def on_slot(self, slot: datetime):
        reading = self.every_min and (slot.minute - reading_offset_min) % self.every_min == 0
        forecasting = self.forecast_every_min and (slot.minute - reading_offset_min) % self.forecast_every_min == 0
        if not reading and not forecasting:
            return

        # Route request failed earlier (e.g. no network at boot): try again
        if not self.points:
            self.points = route_points.load_or_make(self.conn, self.home, self.uni, self.tomtom_key)
            if not self.points:
                return

        if reading:
            self.read_current()
            if slot.hour == 0 and slot.minute == reading_offset_min:
                self.archive_day(slot.date() - timedelta(days=1))

        if forecasting:
            self.refresh_forecast(slot.date())

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

    def refresh_forecast(self, today: date):
        try:
            rows = update_forecast(self.conn, self.points, today)
            logger.info(f"Forecast updated, {describe_forecast(rows)} ({len(rows):,} blocks)")

        except Exception as err:
            logger.error(f"Forecast failed: {describe_error(err)}")

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

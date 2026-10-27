from conditions import route_points, store, weather
from datetime import date
from dotenv import load_dotenv

import database
import logging
import os
import sys

# Fetches the forecast at every weather point once and stores it in weather_forecasts, as the
# collector does every FORECAST_EVERY_MIN, so it can be filled without a restart. Run from
# backend/app:
#   python -m conditions.forecast
# On the server, through the container, which owns the database file:
#   sudo docker compose run --rm collector python -m conditions.forecast

logger = logging.getLogger("commuteiq")


def main():
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    conn = database.connect()
    store.create_tables(conn)

    home = database.get_location(conn, "HOME")
    uni = database.get_location(conn, "UNI")
    if home is None or uni is None:
        sys.exit("Home/university locations are not set. Run: python setup_locations.py")

    if not (points := route_points.load_or_make(conn, home, uni, os.getenv("TOMTOM_API_KEY"))):
        sys.exit("No weather points: the TomTom route request failed (see above)")

    try:
        rows = weather.update_forecast(conn, points, date.today())
    except Exception as err:
        sys.exit(f"Forecast failed: {weather.describe_error(err)}")

    logger.info(f"Forecast stored at {len(points)} points, {weather.describe_forecast(rows)} ({len(rows):,} blocks)")


if __name__ == "__main__":
    main()

from conditions import route_points, store, weather
from datetime import date, datetime, timedelta, timezone
from dotenv import load_dotenv

import argparse
import database
import logging
import os
import sys

# Fills weather_samples with Open-Meteo's 15-minute archive (source 'archive') at every
# weather point, from lookback_days before the first drive-time sample (so a dry spell
# can be measured) through now. Safe to re-run: blocks already archived are replaced, and
# live readings are kept alongside. Run from backend/app:
#   python -m conditions.backfill_weather            # --start / --end YYYY-MM-DD (UTC dates)
# On the server, through the container, which owns the database file:
#   sudo docker compose run --rm collector python -m conditions.backfill_weather

lookback_days: int = 120

# Days per request: Open-Meteo bills each 2 weeks of data as one call per place
chunk_days: int = 14

logger = logging.getLogger("commuteiq")


def parse_date(text: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected YYYY-MM-DD (got {text!r})")


def main():
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description="Fill in archived weather along the route.")
    parser.add_argument("--start", type=parse_date, help=f"first UTC date (default: {lookback_days} days before the first drive-time sample)")
    parser.add_argument("--end", type=parse_date, help="last UTC date (default: today)")
    args = parser.parse_args()

    conn = database.connect()
    store.create_tables(conn)

    home = database.get_location(conn, "HOME")
    uni = database.get_location(conn, "UNI")
    if home is None or uni is None:
        sys.exit("Home/university locations are not set. Run: python setup_locations.py")

    if not (points := route_points.load_or_make(conn, home, uni, os.getenv("TOMTOM_API_KEY"))):
        sys.exit("No weather points: the TomTom route request failed (see above)")

    first = conn.execute("SELECT MIN(departure_date) FROM commute_samples").fetchone()[0]
    today = datetime.now(timezone.utc).date()
    start = args.start or (date.fromisoformat(first) if first else today) - timedelta(days=lookback_days)
    end = args.end or today

    if start > end:
        sys.exit(f"--start {start} is after --end {end}")

    logger.info(f"Archive weather {start} to {end} at {len(points)} points")
    day, total = start, 0

    while day <= end:
        last = min(day + timedelta(days=chunk_days - 1), end)

        try:
            database.record_request(conn, "open_meteo", len(points))
            rows = weather.fetch_archive(points, day, last)
        except Exception as err:
            sys.exit(f"Open-Meteo archive {day} to {last} failed: {weather.describe_error(err)}. "
                     f"Earlier days are saved; re-run with --start {day} to continue.")

        # One commit per chunk keeps write locks short while the collector runs
        store.add_weather(conn, rows, "archive")
        total += len(rows)
        logger.info(f"  {day} to {last}: {len(rows):,} blocks")
        day = last + timedelta(days=1)

    logger.info(f"Done: {total:,} blocks archived")


if __name__ == "__main__":
    main()

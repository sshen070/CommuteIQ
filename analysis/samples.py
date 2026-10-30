from contextlib import closing
from dotenv import load_dotenv
from pathlib import Path

import argparse
import os
import pandas as pd
import re
import sqlite3
import sys

# Shared by plot_drive_times.py and commute_stats.py: loads samples read-only and
# computes per-slot statistics. Run the scripts from this folder (flat imports).

repo_root: Path = Path(__file__).resolve().parents[1]
output_dir: Path = Path(__file__).resolve().parent / "output"

# CommuteIQ/data/commute.db unless DB_PATH is set (same rule as backend/app/database.py)
default_db_path: Path = repo_root / "data" / "commute.db"

# Departures are bucketed into 5-minute slots of local clock time, the collector's own step
slot_min: int = 5

# Below this many days per slot, p90 is roughly the worst day seen, not a real percentile
trust_days: int = 10

# (origin, destination) roles, in display order
legs = [("HOME", "UNI"), ("UNI", "HOME")]
role_names = {"HOME": "Home", "UNI": "Uni"}
toll_names = {0: "Toll-free", 1: "Tolls allowed"}

weekdays = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
day_aliases = {"all": "mon-sun"}

# Days with alike traffic, which can be pooled. Fridays differ from Mon–Thu, and Saturdays
# from Sundays, so --days mon-fri or sat-sun blends two different days into one curve
day_groups = [["Monday", "Tuesday", "Wednesday", "Thursday"], ["Friday"], ["Saturday"], ["Sunday"]]

hhmm_pattern = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def leg_name(origin: str, destination: str) -> str:
    return f"{role_names[origin]} → {role_names[destination]}"


# Minutes after midnight -> "HH:MM" (wraps past midnight)
def hhmm(minutes: float) -> str:
    minutes = int(round(minutes)) % (24 * 60)
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


# argparse type: "HH:MM" -> minutes after midnight
def parse_hhmm(text: str) -> int:
    if not (match := hhmm_pattern.match(text.strip())):
        raise argparse.ArgumentTypeError(f"expected 24-hour HH:MM, e.g. 09:30 (got {text!r})")
    return int(match[1]) * 60 + int(match[2])


# argparse type: "mon-thu", "fri", "sat", "mon,wed,fri", "all" -> weekday names
def parse_days(text: str) -> list[str]:
    text = text.strip().lower()
    text = day_aliases.get(text, text)
    abbrevs = [day[:3].lower() for day in weekdays]

    days: list[str] = []
    for part in text.split(","):
        start, _, end = part.strip().partition("-")
        try:
            first, last = abbrevs.index(start[:3]), abbrevs.index((end or start)[:3])
        except ValueError:
            raise argparse.ArgumentTypeError(f"unknown day in {part!r}; use e.g. mon-thu, fri, sat, sun, all")
        if first > last:
            raise argparse.ArgumentTypeError(f"day range {part!r} runs backwards; write it Monday-first")
        days += [day for day in weekdays[first:last + 1] if day not in days]

    return sorted(days, key=weekdays.index)


# ["Monday", ..., "Thursday"] -> "Mon–Thu"; non-contiguous days are listed
def days_label(days: list[str]) -> str:
    if len(days) == 7:
        return "Every day"
    positions = [weekdays.index(day) for day in days]
    if len(days) > 2 and positions == list(range(positions[0], positions[-1] + 1)):
        return f"{days[0][:3]}–{days[-1][:3]}"
    return ", ".join(day[:3] for day in days)


# --days option shared by both scripts
def add_days_argument(parser: argparse.ArgumentParser):
    parser.add_argument(
        "--days", type=parse_days, default=parse_days("mon-thu"),
        help="weekdays to pool: mon-thu (default), fri, sat or sun; others, e.g. mon-fri or all, blend groups",
    )


# Names of the day groups that days touch, e.g. ["Mon–Thu", "Fri"] for Mon–Fri
def groups_in(days: list[str]) -> list[str]:
    return [days_label(group) for group in day_groups if set(group) & set(days)]


# The database, read-only. Close it as soon as the rows are in memory: an open
# transaction would make the collector's inserts fail with "database is locked"
def connect() -> sqlite3.Connection:
    load_dotenv(repo_root / ".env")
    path = Path((os.getenv("DB_PATH") or "").strip() or default_db_path).resolve()

    if not path.exists():
        sys.exit(f"No database at {path}. Start the collector first (see CLAUDE.md).")
    return sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)


# Samples between the active home and uni (newest location per role) on the given weekdays.
# source is "TomTom", plus "Google" (best guess) rows when google is set
def load(days: list[str], google: bool = False) -> pd.DataFrame:
    with closing(connect()) as conn:
        active = dict(conn.execute("SELECT role, MAX(id) FROM locations GROUP BY role").fetchall())
        columns = "origin_id, destination_id, allow_tolls, departure_date, departure_weekday, departure_time, travel_time_s"
        df = pd.read_sql_query(f"SELECT {columns}, 'TomTom' AS source FROM commute_samples", conn)

        # BEST_GUESS only: it's Google's typical drive, comparable with TomTom. Old
        # PESSIMISTIC rows are a bad-day bound and would pull the curve up
        if google:
            google_df = pd.read_sql_query(
                f"SELECT {columns}, 'Google' AS source FROM google_samples WHERE traffic_model = 'BEST_GUESS'", conn
            )
            df = pd.concat([df, google_df], ignore_index=True)

    if "HOME" not in active or "UNI" not in active:
        sys.exit("Home/university locations are not set. Run setup_locations.py first.")

    # Samples measured from an earlier address belong to a different route
    role_of = {active["HOME"]: "HOME", active["UNI"]: "UNI"}
    current = df["origin_id"].isin(role_of) & df["destination_id"].isin(role_of)
    if older := int((~current).sum()):
        print(f"Note: skipped {older:,} samples measured from an earlier home/uni address.")

    df = df[current].copy()
    df["origin"] = df["origin_id"].map(role_of)
    df["destination"] = df["destination_id"].map(role_of)

    if df.empty:
        sys.exit("No samples yet for the current home/uni. Let the collector run first.")

    selected = df[df["departure_weekday"].isin(days)].copy()
    if selected.empty:
        have = sorted(df["departure_weekday"].unique(), key=weekdays.index)
        sys.exit(f"No samples on {days_label(days)}. Data so far covers: {', '.join(have)}. Try --days all.")

    if len(groups := groups_in(days)) > 1:
        print(f"Note: pooling {', '.join(groups[:-1])} and {groups[-1]}, whose traffic differs."
              " For leave-by times, use one: --days mon-thu, fri, sat or sun.")

    # departure_time is local clock time, so DST changes need no special handling
    clock = selected["departure_time"].str.split(":", expand=True).astype(int)
    selected["dep_min"] = clock[0] * 60 + clock[1]
    selected["slot"] = selected["dep_min"] // slot_min * slot_min
    selected["travel_min"] = selected["travel_time_s"] / 60

    return selected.drop(columns=["origin_id", "destination_id"])


# Per route of the active home/uni (route_profiles, measured by the collector): length_km,
# freeway_km, and freeway_share, the freeway's share of the open-road drive time. Empty
# until the collector has made them
def load_profiles() -> pd.DataFrame:
    with closing(connect()) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'route_profiles'").fetchone():
            return pd.DataFrame(columns=["origin", "destination", "allow_tolls", "length_km", "freeway_km", "freeway_share"])
        active = dict(conn.execute("SELECT role, MAX(id) FROM locations GROUP BY role").fetchall())
        df = pd.read_sql_query("SELECT * FROM route_profiles", conn)

    role_of = {active.get("HOME"): "HOME", active.get("UNI"): "UNI"}
    df = df[df["origin_id"].isin(role_of) & df["destination_id"].isin(role_of)]
    return pd.DataFrame({
        "origin": df["origin_id"].map(role_of),
        "destination": df["destination_id"].map(role_of),
        "allow_tolls": df["allow_tolls"],
        "length_km": df["length_m"] / 1000,
        "freeway_km": df["freeway_m"] / 1000,
        "freeway_share": df["freeway_time_s"] / df["travel_time_s"],
    }).reset_index(drop=True)


# Median and spread of travel time per (source, leg, toll option, departure slot)
def slot_stats(df: pd.DataFrame) -> pd.DataFrame:
    # Grouped quantile instead of a lambda per group: same numbers, fast enough for
    # backtest.py, which recomputes these for every held-out day
    groups = df.groupby(["source", "origin", "destination", "allow_tolls", "slot"])
    return pd.DataFrame({
        "days": groups["departure_date"].nunique(),
        "samples": groups["travel_min"].size(),
        "median": groups["travel_min"].median(),
        "p10": groups["travel_min"].quantile(0.10),
        "p90": groups["travel_min"].quantile(0.90),
    }).reset_index()


def coverage_label(df: pd.DataFrame) -> str:
    dates = pd.to_datetime(df["departure_date"])
    first, last, count = dates.min(), dates.max(), dates.nunique()
    span = f"{first:%b} {first.day}"
    if last != first:
        span += f" – {last:%b} {last.day}"
    return f"{span}, {last:%Y} · {count} day{'s' if count != 1 else ''}"

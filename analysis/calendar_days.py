from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import csv

# Days that may change traffic, from the hand-kept calendar.csv next to this file: public
# holidays, UCR closures, finals and breaks, local K-12 breaks and free-form notes. Nothing
# is stored in the database; features.py tags each collected day when the analysis runs.

calendar_path: Path = Path(__file__).resolve().parent / "calendar.csv"
local_tz = ZoneInfo("America/Los_Angeles")

kinds = ["holiday", "uni_closed", "uni_finals", "uni_break", "k12_break", "note"]

# A day's UCR status, most specific first; weekdays in no range have classes
uni_status = {"uni_closed": "closed", "uni_finals": "finals", "uni_break": "break"}


@dataclass
class Entry:
    start: date
    end: date           # inclusive
    kind: str
    name: str


# calendar.csv rows. Raises ValueError naming the row on an unknown kind or a bad date
def load(path: Path = calendar_path) -> list[Entry]:
    entries = []

    with open(path, newline="") as file:
        lines = (line for line in file if line.strip() and not line.lstrip().startswith("#"))
        for row in csv.DictReader(lines, skipinitialspace=True):
            name = row["name"]
            if row["kind"] not in kinds:
                raise ValueError(f"calendar.csv: unknown kind {row['kind']!r} for {name!r}. Use one of: {', '.join(kinds)}")
            try:
                start = date.fromisoformat(row["start"])
                end = date.fromisoformat(row["end"] or row["start"])
            except ValueError:
                raise ValueError(f"calendar.csv: dates must be YYYY-MM-DD (row {name!r})") from None
            entries.append(Entry(start, end, row["kind"], name))

    return entries


# Everything the calendar says about one local date
def tags(day: date, entries: list[Entry]) -> dict:
    covering = [entry for entry in entries if entry.start <= day <= entry.end]

    def names(kind: str) -> str:
        return "; ".join(entry.name for entry in covering if entry.kind == kind)

    uni = "classes" if day.weekday() < 5 else ""
    for kind, status in uni_status.items():
        if any(entry.kind == kind for entry in covering):
            uni = status
            break

    # 0 on a holiday, 1 the day before (e.g. the Wednesday before Thanksgiving), ...
    upcoming = sorted((max(entry.start, day), entry.name) for entry in entries if entry.kind == "holiday" and entry.end >= day)
    next_day, next_name = upcoming[0] if upcoming else (None, "")

    return {
        "holiday": names("holiday"),
        "uni": uni,
        "k12_break": any(entry.kind == "k12_break" for entry in covering),
        "notes": names("note"),
        "days_to_holiday": (next_day - day).days if next_day else None,
        "next_holiday": next_name,
        "dst_days": days_since_clock_change(day),
    }


def utc_offset(day: date) -> timedelta:
    return datetime.combine(day, time(12), local_tz).utcoffset()


# Days since the clocks last changed (0 on the day itself). Crashes rise for a few days after
def days_since_clock_change(day: date) -> int:
    back = 0
    while utc_offset(day - timedelta(days=back)) == utc_offset(day - timedelta(days=back + 1)):
        back += 1
    return back


# The first day on or after a date when the clocks change
def next_clock_change(day: date) -> date:
    while utc_offset(day) == utc_offset(day - timedelta(days=1)):
        day += timedelta(days=1)
    return day

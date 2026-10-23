from datetime import date, timedelta

import argparse
import calendar_days
import features
import os
import pandas as pd
import samples
import sys

# One line per collected day: calendar, weather on the route, and how far each direction's
# drives differed from an average day. Rainy, holiday or glare days stand out here before
# there's enough of them to model. Also writes, for that later modeling (raw units):
#   output/day_features.csv     the same, per day
#   output/sample_features.csv  every TomTom sample with its weather, sun and calendar

# A drive is compared with the average of the other days in its group that left at the
# same time, to the collector's 5-minute mark. Leaving the day itself out keeps a bad day
# from pulling its own baseline toward it. A group's first weeks have only a day or two to
# compare with, so the table shows how many (base_days) rather than leaving them out
mark_min: int = 5
min_other_days: int = 1

# Days are compared only within their group (samples.day_groups: Mon–Thu, Fri, Sat, Sun)
group_of = {day: samples.days_label(group) for group in samples.day_groups for day in group}

# Commute windows, as in the collector (main.py): WINDOW_HOME_TO_UNI / WINDOW_UNI_TO_HOME
# in .env, or these defaults. Only drives inside them are compared, so Home -> Uni isn't
# judged by its afternoon peak, when you'd be driving the other way
default_windows = {("HOME", "UNI"): "05:00-14:00", ("UNI", "HOME"): "12:00-21:00"}


def print_table(header: list[str], rows: list[list[str]], indent: str = "  "):
    widths = [max(len(row[i]) for row in [header, *rows]) for i in range(len(header))]
    for row in [header, *rows]:
        print(indent + "   ".join(cell.ljust(width) for cell, width in zip(row, widths)).rstrip())


# A direction's commute window as minutes after midnight [start, end). A window that's
# "off" in .env falls back to the default, since older samples may still be inside it
def window(origin: str, destination: str) -> tuple[int, int]:
    value = (os.getenv(f"WINDOW_{origin}_TO_{destination}") or "").strip()
    if not value or value.lower() == "off":
        value = default_windows[(origin, destination)]

    start, _, end = value.partition("-")
    return samples.parse_hhmm(start), samples.parse_hhmm(end)


# Each sample against the average of the other days in its group that left at the same
# 5-minute mark (avg_min, diff_min, base_days), and whether it's inside its commute window
def add_diff(df: pd.DataFrame) -> pd.DataFrame:
    df = df.assign(day_group=df["departure_weekday"].map(group_of), in_window=False, mark=df["dep_min"] // mark_min * mark_min)

    for origin, destination in samples.legs:
        start, end = window(origin, destination)
        leg = (df["origin"] == origin) & (df["destination"] == destination)
        df.loc[leg & (df["dep_min"] >= start) & (df["dep_min"] < end), "in_window"] = True

    same_time = df.groupby(["origin", "destination", "allow_tolls", "day_group", "mark"])["travel_min"]
    df["base_days"] = same_time.transform("count") - 1
    df["avg_min"] = ((same_time.transform("sum") - df["travel_min"]) / df["base_days"]).where(df["base_days"] >= min_other_days)
    df["diff_min"] = df["travel_min"] - df["avg_min"]
    return df


# Per day and direction (toll-free, inside the commute window), in minutes: the average
# difference from the other days (+ slower, - faster), and its root mean square (RMSE),
# the typical size of the difference either way. A day that's 5 minutes slow all day and
# one that swings 5 minutes either side share an RMSE, but not an average difference.
# base_days is how many other days the day was typically compared with. Also, from the
# same drives (with or without a baseline), the drives that met rain and the wind on them
def leg_summary(df: pd.DataFrame) -> pd.DataFrame:
    driven = df[(df["allow_tolls"] == 0) & df["in_window"]]
    compared = driven[driven["diff_min"].notna()]
    out = pd.DataFrame(index=pd.Index(sorted(df["departure_date"].unique()), name="date"))
    out["base_days"] = compared.groupby("departure_date")["base_days"].median()

    for origin, destination in samples.legs:
        leg = compared[(compared["origin"] == origin) & (compared["destination"] == destination)]
        prefix = f"{origin.lower()}_to_{destination.lower()}"

        by_day = leg.groupby("departure_date")["diff_min"]
        out[f"{prefix}_diff_min"] = by_day.mean()
        out[f"{prefix}_rmse_min"] = (leg["diff_min"] ** 2).groupby(leg["departure_date"]).mean() ** 0.5
        out[f"{prefix}_compared"] = by_day.size()

        rainy = driven[
            (driven["origin"] == origin) & (driven["destination"] == destination) & driven["rain_headwind_kmh"].notna()
        ].groupby("departure_date")
        out[f"{prefix}_rain_drives"] = rainy.size().reindex(out.index, fill_value=0)
        out[f"{prefix}_rain_headwind_kmh"] = rainy["rain_headwind_kmh"].mean()
        out[f"{prefix}_rain_cross_gust_kmh"] = rainy["rain_cross_gust_kmh"].max()

    return out


def number(value, spec: str) -> str:
    return "–" if pd.isna(value) else format(value, spec)


# Raw units -> US units for display
def inches(mm):
    return mm / 25.4


def miles(km):
    return km / 1.609344


def fahrenheit(celsius):
    return celsius * 9 / 5 + 32


def calendar_text(row: pd.Series) -> str:
    parts = []
    if row["uni"] not in ("classes", ""):
        parts.append(f"UCR {row['uni']}")
    if row["holiday"]:
        parts.append(row["holiday"])
    elif row["days_to_holiday"] == 1:
        parts.append(f"before {row['next_holiday']}")
    if row["k12_break"]:
        parts.append("K-12 break")
    if row["dst_days"] < 7:
        parts.append("clocks changed" if row["dst_days"] == 0 else f"clocks changed {row['dst_days']} d ago")
    if row["notes"]:
        parts.append(row["notes"])
    return "; ".join(parts) or "–"


# {1065: 5, 1070: 10, 1100: 5} (departure minute: glare km) -> "17:45–17:55 (10 km), 18:20–18:25 (5 km)"
def runs(glare: dict[int, float]) -> str:
    if not glare:
        return "none"
    minutes = sorted(glare)
    spans, start, previous = [], minutes[0], minutes[0]
    for minute in minutes[1:]:
        if minute != previous + 5:
            spans.append((start, previous))
            start = minute
        previous = minute
    spans.append((start, previous))
    return ", ".join(
        f"{samples.hhmm(a)}–{samples.hhmm(b + 5)} ({max(km for m, km in glare.items() if a <= m <= b):.0f} km)"
        for a, b in spans
    )


# Per day with rain on commute drives: for each direction, the wind against the car where
# it was raining. Shows whether one direction tends to drive into the rain
def print_rain_wind(days: pd.DataFrame):
    print("\nRain and wind on toll-free drives inside the commute windows, counted only where rain was falling."
          "\nHeadwind: per drive, the average along its rainy stretches (+ into the wind, - tailwind); then"
          " averaged over the day's drives in rain.")

    prefixes = [(f"{origin.lower()}_to_{destination.lower()}", samples.leg_name(origin, destination))
                for origin, destination in samples.legs]
    rainy = days[(days[[f"{prefix}_rain_drives" for prefix, _ in prefixes]] > 0).any(axis=1)]
    if rainy.empty:
        print("  No rain on commute drives yet.")
        return

    rows = []
    for day, row in rainy.iterrows():
        when = date.fromisoformat(day)
        for i, (prefix, name) in enumerate(prefixes):
            rows.append([
                f"{when:%a %b} {when.day}" if i == 0 else "",
                name, f"{int(row[f'{prefix}_rain_drives'])}",
                number(miles(row[f"{prefix}_rain_headwind_kmh"]), "+.0f"),
                number(miles(row[f"{prefix}_rain_cross_gust_kmh"]), ".0f"),
            ])
    print_table(["Date", "Direction", "Drives in rain", "Headwind mph", "Crosswind gust mph"], rows)


# Glare departure times on the latest day, and on the first weekday after the clocks next
# change, when the commute meets the low sun an hour earlier
def print_glare(df: pd.DataFrame, line: pd.DataFrame, latest: date):
    change = calendar_days.next_clock_change(latest + timedelta(days=1))
    after = change + timedelta(days=7 - change.weekday() if change.weekday() >= 5 else 0)

    print(f"\nSun glare: departures that drive into a sun under {features.glare_max_elev:.0f}° and within "
          f"{features.glare_max_offset:.0f}° of the heading on each 5 km stretch of road (most km of glare in each window)")

    for label, day in [("latest day", latest), ("after the clocks change", after)]:
        cells = []
        for origin, destination in samples.legs:
            leg = df[(df["origin"] == origin) & (df["destination"] == destination) & (df["allow_tolls"] == 0)]
            if not leg.empty:
                typical = leg.groupby("slot")["travel_min"].median()
                glare = features.glare_departures(day, origin, line, typical)
                cells.append(f"{samples.leg_name(origin, destination)} {runs(glare)}")
        print(f"  {day:%a %b} {day.day} ({label}): {' · '.join(cells)}")


def main():
    parser = argparse.ArgumentParser(description="Calendar, weather and difference from an average day, for every collected day.")
    parser.add_argument(
        "--days", type=samples.parse_days, default=samples.parse_days("all"),
        help="weekdays to include, e.g. mon-thu, fri, sat, sun, all (default); each day is still compared only within its group",
    )
    args = parser.parse_args()

    df, points, weather = features.load()
    df = df[df["departure_weekday"].isin(args.days)]
    if df.empty:
        sys.exit(f"No samples on {samples.days_label(args.days)} yet.")

    df, days = features.build(df, points, weather)
    df = add_diff(df)
    days = days.join(leg_summary(df))
    days.insert(0, "weekday", pd.to_datetime(days.index).strftime("%a"))

    first, last = pd.to_datetime(days.index[[0, -1]])
    hours = f"{features.day_hours[0]:02}:00–{features.day_hours[1]:02}:00"
    windows = ", ".join(
        f"{samples.leg_name(origin, destination)} {'–'.join(samples.hhmm(m) for m in window(origin, destination))}"
        for origin, destination in samples.legs
    )
    print(f"{samples.days_label(args.days)} · {first:%b} {first.day} – {last:%b} {last.day}, {last:%Y} · {len(days)} days")
    live = weather.loc[weather["source"] == "live", "time"].min()
    if pd.notna(live):
        live = live.tz_convert(features.local_tz)
    since = f"live since {live:%b} {live.day} {live:%H:%M}, archive before" if pd.notna(live) else "archive only"
    print(f"Weather {hours} at {len(points)} points along the route (Open-Meteo, {since}): rain at the")
    print("wettest point, hours with rain anywhere, and the worst visibility and gusts. Visibility over 10 mi shows as 10+.")
    groups = ", ".join(samples.days_label(group) for group in samples.day_groups)
    print(f"Diff: average minutes slower (+) or faster (-) than the other days in its group ({groups})")
    print(f"at the same departure times, inside each commute window ({windows}).")
    print("RMSE: root mean squared difference, the typical size of the gap either way. Base: how many other days")
    print("that is. With only 1, the two days' diffs mirror each other. TomTom, toll-free.\n")

    legs = [(f"{origin.lower()}_to_{destination.lower()}", samples.leg_name(origin, destination)) for origin, destination in samples.legs]
    header = ["Date", "Day", "Rain in", "Wet h", "Dry d", "Vis mi", "Gust mph", "High °F", "Base"]
    for _, name in legs:
        header += [f"{name} diff", "RMSE"]
    header.append("Calendar")

    rows = []
    for day, row in days.iterrows():
        dry = number(row["dry_days"], ".0f") + ("+" if row["dry_days"] >= features.dry_cap_days else "")
        visibility = miles(row["min_visibility_km"])
        cells = [
            day, row["weekday"],
            number(inches(row["rain_mm"]), ".2f"), number(row["wet_hours"], "g"), dry,
            "10+" if visibility >= 10 else number(visibility, ".1f"),
            number(miles(row["max_gust_kmh"]), ".0f"), number(fahrenheit(row["high_c"]), ".0f"),
            number(row["base_days"], ".0f"),
        ]
        for prefix, _ in legs:
            cells += [number(row[f"{prefix}_diff_min"], "+.1f"), number(row[f"{prefix}_rmse_min"], ".1f")]
        cells.append(calendar_text(row))
        rows.append(cells)

    print_table(header, rows)
    print_rain_wind(days)
    print_glare(df, features.stretches(points), date.fromisoformat(days.index[-1]))

    # Files for later modeling
    samples.output_dir.mkdir(exist_ok=True)
    days_path = samples.output_dir / "day_features.csv"
    days.round(3).to_csv(days_path)

    samples_path = samples.output_dir / "sample_features.csv"
    table = df.assign(
        direction=[samples.leg_name(o, d) for o, d in zip(df["origin"], df["destination"])],
        route=df["allow_tolls"].map(samples.toll_names),
    )
    columns = [
        "departure_date", "departure_weekday", "departure_time", "direction", "route",
        "travel_min", "day_group", "base_days", "avg_min", "diff_min", "in_window",
        "precip_mm", "wet_share", "wet_road_mm", "weather_code", "visibility_km", "gust_kmh",
        "rain_headwind_kmh", "rain_cross_gust_kmh", "temp_c",
        "cloud_cover_pct", "dry_days", "sun_elev", "glare_km", "dark_km",
        "holiday", "days_to_holiday", "uni", "k12_break", "notes", "dst_days",
    ]
    table[columns].sort_values(["departure_date", "departure_time", "direction", "route"]).round(3).to_csv(samples_path, index=False)

    print(f"\nPer day: {days_path.relative_to(samples.repo_root)}")
    print(f"Per sample: {samples_path.relative_to(samples.repo_root)}")


if __name__ == "__main__":
    main()

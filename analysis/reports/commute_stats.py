from core import samples
from core.samples import print_table
from datetime import date
from models import advice, driving_styles
from models.advice import lookback_min, max_wait_min, near_best_min, worth_waiting_min

import argparse
import pandas as pd
import sys

# Statistics behind the departure decision, per direction, toll option and driving style
# (driving_styles.py: Google-led blends of Google and TomTom, matched by day and slot),
# scaled per slot to the weather (weather_model.py): the stored forecast for --date, or a
# what-if. The rules and the weather scaling are in advice.py:
#   - Home -> Uni: latest safe departure for each class start (p90 drive time)
#   - Uni -> Home: leave right after class, or wait at uni for traffic to ease?
# Also writes output/slot_stats.csv with every slot's numbers, per source and style.

# A slot is "slow" when its median is this much above the quickest slot's
slow_factor: float = 1.25


# Consecutive slots -> "06:15–09:30, 14:00–19:15"
def windows(slots: pd.Index) -> str:
    if slots.empty:
        return "none"
    runs, start, previous = [], slots[0], slots[0]
    for slot in slots[1:]:
        if slot != previous + samples.slot_min:
            runs.append((start, previous))
            start = slot
        previous = slot
    runs.append((start, previous))
    return ", ".join(f"{samples.hhmm(a)}–{samples.hhmm(b + samples.slot_min)}" for a, b in runs)


# def describe_leg(stats: pd.DataFrame, origin: str, destination: str, toll_options: list[int]):
#     print(f"\n{samples.leg_name(origin, destination)}")

#     for tolls in toll_options:
#         series = series_for(stats, origin, destination, tolls)
#         quickest, slowest = series["median"].idxmin(), series["median"].idxmax()
#         slow = series.index[series["median"] >= series.at[quickest, "median"] * slow_factor]

#         print(f"  {samples.toll_names[tolls]}")
#         print(f"    Quickest     {samples.hhmm(quickest)}  {series.at[quickest, 'median']:.0f} min median")
#         print(f"    Slowest      {samples.hhmm(slowest)}  {series.at[slowest, 'median']:.0f} min median"
#               f" (p90 {series.at[slowest, 'p90']:.0f})")
#         print(f"    Slow periods {windows(slow)}  (median {slow_factor - 1:.0%}+ over quickest)")

#     # Toll savings, slot by slot, where both options were sampled
#     if len(toll_options) == 2:
#         free, toll = (series_for(stats, origin, destination, tolls)["median"] for tolls in (0, 1))
#         saved = (free - toll).dropna()
#         if not saved.empty:
#             print(f"  Tolls save {saved.median():.0f} min in a typical slot,"
#                   f" up to {saved.max():.0f} min at {samples.hhmm(saved.idxmax())}")


def main():
    parser = argparse.ArgumentParser(description="Departure statistics and leave-by times for both directions.")
    samples.add_days_argument(parser)
    advice.add_advice_arguments(parser)
    parser.add_argument("--rain", type=float, default=0, metavar="IN/H",
                        help="what-if: rain falling during the drive, in inches an hour (so far any rate counts the same)")
    parser.add_argument("--wet-road", action="store_true", help="what-if: the rain has stopped but the road is still wet")
    parser.add_argument("--gust", type=float, default=0, metavar="MPH", help="what-if: the strongest gust during the drive")
    parser.add_argument("--date", type=advice.parse_date, default=date.today(), metavar="DAY",
                        help="the day whose stored forecast to use: today (default), tomorrow or YYYY-MM-DD")
    parser.add_argument("--no-forecast", action="store_true", help="use the drives as sampled, without the forecast")
    args = parser.parse_args()
    chosen = [style for style in driving_styles.styles if style in args.style]
    if args.rain < 0 or args.gust < 0:
        parser.error("--rain and --gust can't be negative")

    try:
        basis = advice.basis(args.days, None if args.no_forecast else args.date, args.rain, args.wet_road, args.gust)
    except advice.NoAdvice as err:
        sys.exit(str(err))
    df, profiles, styled, stats, weather_note = basis.df, basis.profiles, basis.styled, basis.stats, basis.weather_note
    style_names = [style.name for style in chosen]

    # Coverage first, so every number below is read in context
    per_day = styled.drop_duplicates("departure_date")["departure_weekday"].value_counts()
    day_counts = ", ".join(f"{day[:3]} ×{per_day[day]}" for day in samples.weekdays if day in per_day)
    pairs = len(styled) // len(driving_styles.styles)
    print(f"{samples.days_label(args.days)} · {samples.coverage_label(styled)} ({day_counts})"
          f" · {pairs:,} Google/TomTom pair{'s' if pairs != 1 else ''}")
    if tomtom_only := sorted(set(df["departure_date"]) - set(styled["departure_date"])):
        print(f"Note: left out {len(tomtom_only)} day{'s' if len(tomtom_only) != 1 else ''} without both"
              f" Google and TomTom samples ({', '.join(tomtom_only)}).")
    print(f"Weather: {weather_note}.")
    if args.date.strftime("%A") not in args.days:
        print(f"Note: {args.date:%A} isn't among the pooled days ({samples.days_label(args.days)});"
              f" try --days {args.date.strftime('%a').lower()}.")

    print(f"\nDriving style{'s' if len(chosen) != 1 else ''} (drive times lean on Google; see driving_styles.py):")
    for style in chosen:
        print(f"  {style.name:<11} {style.description}")

    print(f"\nFreeway per route: cruising speed matters there. Congestion runs from 0 while freeway traffic"
          f" keeps its open-road speed\nto 1 at {driving_styles.congested_mph:g} mph average, where Caltrans counts"
          f" a freeway as congested (mostly stop-and-go).")
    open_mph = styled.groupby(driving_styles.route_keys)["open_mph"].first()
    rows = []
    for (origin, destination, tolls), mph in open_mph.items():
        profile = profiles.set_index(driving_styles.route_keys).loc[(origin, destination, tolls)]
        rows.append([
            samples.leg_name(origin, destination), samples.toll_names[tolls],
            f"{profile['freeway_km'] / driving_styles.km_per_mile:.1f} of {profile['length_km'] / driving_styles.km_per_mile:.1f} mi",
            f"{mph:.0f} mph",
        ])
    print_table(["Direction", "Route", "Freeway", "Open-road speed"], rows)

    most_days = int(stats["days"].max())
    if most_days < samples.trust_days:
        print(f"\nEarly data: at most {most_days} day{'s' if most_days != 1 else ''} per slot, so p90 is close to the"
              f" worst day seen. Treat leave-by times as rough until slots have {samples.trust_days}+ days.")

    # A slot with fewer days than the rest (collector downtime, partial day) gets a *
    thin_days = min(samples.trust_days, most_days)

    def mark(row: pd.Series) -> str:
        return "*" if row["days"] < thin_days else ""

    # Home -> Uni: latest safe departure
    for tolls in sorted(styled[styled["origin"] == "HOME"]["allow_tolls"].unique()):
        print(f"\nLeave home by, {samples.toll_names[tolls].lower()}: latest departure that still arrives"
              f" {args.buffer} min before class on a bad day (p90).")
        print(f"Each cell: leave-by time · p90 / median drive time; \"or\" = leave earlier to save {worth_waiting_min}+ min"
              f" of driving: the latest departure within {near_best_min} min of the best drive in the {lookback_min // 60} h"
              f" before class.")
        rows = []
        for class_start in sorted(args.class_start):
            row = [samples.hhmm(class_start)]
            for name in style_names:
                series = basis.series("HOME", "UNI", tolls, name)
                result = advice.leave_by(series, class_start, args.buffer)
                if isinstance(result, str):
                    row.append(result)
                    continue
                cell = f"{samples.hhmm(result.name)} · {result['p90']:.0f} / {result['median']:.0f} min{mark(result)}"
                if (earlier := advice.leave_earlier(series, result, class_start)) is not None:
                    cell += f", or {samples.hhmm(earlier.name)} · {earlier['median']:.0f} min{mark(earlier)}"
                row.append(cell)
            rows.append(row)
        print_table(["Class", *style_names], rows)

    # Uni -> Home: go now, or wait it out?
    for tolls in sorted(styled[styled["origin"] == "UNI"]["allow_tolls"].unique()):
        print(f"\nHeading home, {samples.toll_names[tolls].lower()}: leave right after class, or wait up to"
              f" {max_wait_min // 60} h at uni for traffic to ease?")
        print(f"Each cell: when to leave · median drive · typical arrival home. Waiting is suggested only"
              f" when it saves {worth_waiting_min}+ min of driving: the first departure within {near_best_min} min of"
              f" the best drive in the next {max_wait_min // 60} h.")
        rows = []
        for class_end in sorted(args.class_end):
            row = [samples.hhmm(class_end)]
            for name in style_names:
                series = basis.series("UNI", "HOME", tolls, name)
                if (result := advice.heading_home(series, class_end)) is None:
                    row.append("no data")
                    continue
                now, best = result
                if best.name == now.name:
                    row.append(f"right away · {now['median']:.0f} min{mark(now)} · home"
                               f" {samples.hhmm(max(now.name, class_end) + now['median'])}")
                else:
                    row.append(f"wait to {samples.hhmm(best.name)} · {best['median']:.0f} min{mark(best)} · home"
                               f" {samples.hhmm(best.name + best['median'])}")
            rows.append(row)
        print_table(["Class ends", *style_names], rows)

    if (stats["days"] < thin_days).any():
        print(f"\n* fewer than {thin_days} days of data in this slot (collector downtime or a partial day)")

    # Every slot, for digging in further: each source on all its days, then each style
    samples.output_dir.mkdir(exist_ok=True)
    path = samples.output_dir / "slot_stats.csv"
    every = pd.concat([samples.slot_stats(df), stats], ignore_index=True)
    table = every.assign(
        direction=[samples.leg_name(o, d) for o, d in zip(every["origin"], every["destination"])],
        route=every["allow_tolls"].map(samples.toll_names),
        slot=every["slot"].map(samples.hhmm),
    )
    columns = ["source", "direction", "route", "slot", "days", "samples", "median", "p10", "p90"]
    table[columns].round(1).to_csv(path, index=False)
    print(f"\nPer-slot numbers: {path.relative_to(samples.repo_root)}")

if __name__ == "__main__":
    main()

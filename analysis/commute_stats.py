import argparse
import driving_styles
import pandas as pd
import samples
import sys

# Statistics behind the departure decision, per direction, toll option and driving style
# (driving_styles.py: Google-led blends of Google and TomTom, matched by day and slot):
#   - Home -> Uni: latest safe departure for each class start (p90 drive time)
#   - Uni -> Home: leave right after class, or wait at uni for traffic to ease?
# Also writes output/slot_stats.csv with every slot's numbers, per source and style.

# How far before class to look for a departure slot
lookback_min: int = 4 * 60

# How long you'd stay at uni to dodge traffic, and the least drive time worth waiting for.
# When waiting is worth it, leave at the first slot within near_best_min of the best one.
max_wait_min: int = 3 * 60
worth_waiting_min: int = 10
near_best_min: int = 3

# A slot is "slow" when its median is this much above the quickest slot's
slow_factor: float = 1.25

default_class_starts = list(range(8 * 60, 12 * 60 + 1, 30))   # 08:00, 08:30, ... 12:00
default_class_ends = list(range(12 * 60, 18 * 60 + 1, 60))    # 12:00, 13:00, ... 18:00


def print_table(header: list[str], rows: list[list[str]], indent: str = "  "):
    widths = [max(len(row[i]) for row in [header, *rows]) for i in range(len(header))]
    for row in [header, *rows]:
        print(indent + "   ".join(cell.ljust(width) for cell, width in zip(row, widths)).rstrip())


# argparse type: "aggressive", "Defensive", "rel" -> that driving style
def parse_style(text: str) -> driving_styles.Style:
    matches = [style for style in driving_styles.styles if style.name.lower().startswith(text.strip().lower())]
    if len(matches) != 1:
        names = ", ".join(style.name.lower() for style in driving_styles.styles)
        raise argparse.ArgumentTypeError(f"unknown driving style {text!r}; use {names}")
    return matches[0]


# One leg, toll option and source or style, indexed by slot (minutes after midnight)
def series_for(stats: pd.DataFrame, origin: str, destination: str, tolls: int, source: str) -> pd.DataFrame:
    rows = stats[
        (stats["source"] == source) & (stats["origin"] == origin) & (stats["destination"] == destination)
        & (stats["allow_tolls"] == tolls)
    ]
    return rows.set_index("slot").sort_index()


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


# Latest slot d where d and every earlier slot in the lookback window arrive by the
# deadline at p90. Requiring the earlier slots too keeps one lucky late slot
# (noise in a small sample) from being recommended over an unlucky earlier one.
def leave_by(series: pd.DataFrame, class_start: int, buffer: int) -> pd.Series | str:
    deadline = class_start - buffer
    window = series[(series.index >= class_start - lookback_min) & (series.index < deadline)]

    if window.empty:
        return "no data"

    on_time = (window.index + window["p90"] <= deadline).to_numpy()
    if not on_time[0]:
        return f"before {samples.hhmm(window.index[0])}"

    first_late = on_time.argmin() if not on_time.all() else len(on_time)
    return window.iloc[first_late - 1]


# Leave right after class, or wait for the first slot (within max_wait_min) that is
# near the best drive time, if that cuts at least worth_waiting_min of driving
def heading_home(series: pd.DataFrame, class_end: int) -> tuple[pd.Series, pd.Series] | None:
    # "Leave then" must be the class-end slot itself, not the next slot that has data
    first_slot = class_end // samples.slot_min * samples.slot_min
    if first_slot not in series.index:
        return None

    window = series[(series.index >= first_slot) & (series.index <= class_end + max_wait_min)]

    right_away = window.iloc[0]
    best = window[window["median"] <= window["median"].min() + near_best_min].iloc[0]

    # Not worth staying unless it saves real time
    if right_away["median"] - best["median"] < worth_waiting_min:
        best = right_away
    return right_away, best


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
    parser.add_argument("--class-start", type=samples.parse_hhmm, nargs="+", default=default_class_starts,
                        metavar="HH:MM", help="first class start time(s); default 08:00 to 12:00 every 30 min")
    parser.add_argument("--class-end", type=samples.parse_hhmm, nargs="+", default=default_class_ends,
                        metavar="HH:MM", help="last class end time(s); default 12:00 to 18:00 hourly")
    parser.add_argument("--buffer", type=int, default=15, metavar="MIN",
                        help="minutes to park and walk to class (default 15)")
    parser.add_argument("--style", type=parse_style, nargs="+", default=driving_styles.styles, metavar="STYLE",
                        help="driving style(s) to show: aggressive, defensive, relaxed (default: all)")
    args = parser.parse_args()
    chosen = [style for style in driving_styles.styles if style in args.style]

    df = samples.load(args.days, google=True)
    profiles = samples.load_profiles()
    routes = df[driving_styles.route_keys].drop_duplicates()
    if missing := len(routes.merge(profiles, on=driving_styles.route_keys, how="left").query("freeway_km.isna()")):
        sys.exit(f"{missing} route{'s have' if missing != 1 else ' has'} no freeway profile yet. Run python route_profile.py"
                 " from backend/app (see its header), or restart an updated collector.")
    styled = driving_styles.style_samples(df, profiles)
    if styled.empty:
        sys.exit(f"No {samples.days_label(args.days)} days with both Google and TomTom samples yet,"
                 " so driving styles can't be computed. Add GOOGLE_CLOUD_API_KEY to .env and let the collector run.")
    stats = samples.slot_stats(styled)
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
        print("Each cell: leave-by time · p90 / median drive time.")
        rows = []
        for class_start in sorted(args.class_start):
            row = [samples.hhmm(class_start)]
            for name in style_names:
                result = leave_by(series_for(stats, "HOME", "UNI", tolls, name), class_start, args.buffer)
                if isinstance(result, str):
                    row.append(result)
                else:
                    row.append(f"{samples.hhmm(result.name)} · {result['p90']:.0f} / {result['median']:.0f} min{mark(result)}")
            rows.append(row)
        print_table(["Class", *style_names], rows)

    # Uni -> Home: go now, or wait it out?
    for tolls in sorted(styled[styled["origin"] == "UNI"]["allow_tolls"].unique()):
        print(f"\nHeading home, {samples.toll_names[tolls].lower()}: leave right after class, or wait up to"
              f" {max_wait_min // 60} h at uni for traffic to ease?")
        print(f"Each cell: when to leave · median drive · typical arrival home. Waiting is suggested only"
              f" when it saves {worth_waiting_min}+ min of driving.")
        rows = []
        for class_end in sorted(args.class_end):
            row = [samples.hhmm(class_end)]
            for name in style_names:
                if (result := heading_home(series_for(stats, "UNI", "HOME", tolls, name), class_end)) is None:
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

import argparse
import commute_stats
import day_log
import driving_styles
import features
import numpy as np
import pandas as pd
import samples
import sys
import weather_model

# Backtest of commute_stats.py's advice: for each held-out day, the advice is worked out from
# other days in its group only (samples.day_groups), then checked against that day's own
# drives. Two ways to pick the other days:
#   forward         only earlier days, as the advice would really have been given
#   leave-one-out   every other day, earlier and later, so every day gets tested
# Two kinds of advice:
#   weather-aware   commute_stats.py's with the weather given: the other days' drives scaled
#                   by the held-out day's own weather at each slot, as if the forecast were
#                   right. weather_model.py is refitted without the held-out day (forward:
#                   on earlier days only) but on every weekday, as commute_stats.py fits it
#                   on every day; the weekday is already in its baseline
#   weather-blind   the other days' drives as sampled, whatever the held-out day's weather
# Checks:
#   - leave home by: does the held-out day's drive at the advised slot arrive in time?
#   - leave earlier ("or"): in time too, and how much driving did it really save?
#   - heading home: when the advice is to wait, how much driving did waiting really save?
#   - every slot in the commute windows: was the drive under the advised p90, and how far
#     from the advised median? This checks the estimate itself, whatever the class times
# A day's "own drive" is the style's estimate from Google and TomTom; there are no real
# arrival times yet. Style times, and the weather model's dry-day baseline, are worked out
# once from all days: one day barely moves them.
# Writes output/backtest.csv (every advice check) and output/backtest_slots.csv (every slot).

# Forward testing starts once a day has this many earlier days in its group
default_min_days: int = 5

slot_keys = ["source", "origin", "destination", "allow_tolls", "slot"]


# {held-out day: days to learn from}, for one way of picking them
def folds(dates: list[str], weekday_of: dict[str, str], mode: str, min_days: int) -> dict[str, list[str]]:
    out = {}
    for test in dates:
        group = day_log.group_of[weekday_of[test]]
        train = [day for day in dates if day != test and day_log.group_of[weekday_of[day]] == group]
        if mode == "forward":
            train = [day for day in train if day < test]
            if len(train) < min_days:
                continue
        out[test] = train
    return out


# The held-out day's weather per (origin, allow_tolls, slot), from its Google drives: the
# weather model's multiplier there (1 without a model), and whether it rained, the road was
# wet or gusts passed weather_model.gust_start_mph
def day_weather(weather: pd.DataFrame | None, test: str, model) -> pd.DataFrame:
    if weather is None:
        return pd.DataFrame(columns=["factor", "stormy"])
    day = weather[weather["departure_date"] == test].set_index(["origin", "allow_tolls", "slot"])
    x = weather_model.terms(day["rain_mm_h"].fillna(0), day["wet_road_mm"].fillna(0), day["gust_kmh"].fillna(0))
    factor = 1 + x @ model.coef if model else np.ones(len(day))
    return pd.DataFrame({"factor": factor, "stormy": (x > 0).any(axis=1)}, index=day.index)


# A leg's advised statistics; weather-aware advice scales them by the day's weather at each slot
def advised(stats: pd.DataFrame, origin: str, tolls: int, name: str, conditions: pd.DataFrame, scale: bool) -> pd.DataFrame:
    destination = "UNI" if origin == "HOME" else "HOME"
    series = commute_stats.series_for(stats, origin, destination, tolls, name)
    if not scale:
        return series
    factor = pd.Series({slot: f for (o, t, slot), f in conditions["factor"].items() if o == origin and t == tolls})
    return weather_model.scale(series, factor)


# Every advice check for one held-out day
def check_day(stats: pd.DataFrame, day: pd.DataFrame, conditions: pd.DataFrame, names: list[str], args,
              base: dict, scale: bool) -> list[dict]:
    own = day.set_index(["source", "origin", "allow_tolls", "slot"])["travel_min"]
    stormy = conditions["stormy"]
    rows = []

    for tolls in sorted(day["allow_tolls"].unique()):
        for name in names:
            row = {**base, "style": name, "allow_tolls": tolls}

            home = advised(stats, "HOME", tolls, name, conditions, scale)
            for class_start in args.class_start:
                latest = commute_stats.leave_by(home, class_start, args.buffer)
                if isinstance(latest, str) or (drive := own.get((name, "HOME", tolls, latest.name))) is None:
                    continue
                deadline = class_start - args.buffer
                rows.append({**row, "rule": "leave by", "time": class_start, "slot": latest.name,
                             "stormy": bool(stormy.get(("HOME", tolls, latest.name), False)),
                             "predicted_min": latest["p90"], "actual_min": drive,
                             "late_min": latest.name + drive - deadline})

                earlier = commute_stats.leave_earlier(home, latest, class_start)
                if earlier is not None and (early_drive := own.get((name, "HOME", tolls, earlier.name))) is not None:
                    rows.append({**row, "rule": "leave earlier", "time": class_start, "slot": earlier.name,
                                 "stormy": bool(stormy.get(("HOME", tolls, earlier.name), False)),
                                 "predicted_min": earlier["median"], "actual_min": early_drive,
                                 "late_min": earlier.name + early_drive - deadline,
                                 "saved_predicted_min": latest["median"] - earlier["median"],
                                 "saved_actual_min": drive - early_drive})

            uni = advised(stats, "UNI", tolls, name, conditions, scale)
            for class_end in args.class_end:
                if (result := commute_stats.heading_home(uni, class_end)) is None:
                    continue
                now, best = result
                if best.name == now.name:
                    continue
                now_drive = own.get((name, "UNI", tolls, now.name))
                best_drive = own.get((name, "UNI", tolls, best.name))
                if now_drive is None or best_drive is None:
                    continue
                rows.append({**row, "rule": "wait to go home", "time": class_end, "slot": best.name,
                             "stormy": bool(stormy.get(("UNI", tolls, best.name), False)),
                             "predicted_min": best["median"], "actual_min": best_drive,
                             "saved_predicted_min": now["median"] - best["median"],
                             "saved_actual_min": now_drive - best_drive})

    return rows


# Every slot of the held-out day inside the commute windows, against the advised median and p90
def check_slots(stats: pd.DataFrame, day: pd.DataFrame, conditions: pd.DataFrame, base: dict, scale: bool) -> pd.DataFrame:
    checked = day.merge(stats[[*slot_keys, "median", "p90"]], on=slot_keys)
    window = {leg: day_log.window(*leg) for leg in samples.legs}
    start = np.array([window[(o, d)][0] for o, d in zip(checked["origin"], checked["destination"])])
    end = np.array([window[(o, d)][1] for o, d in zip(checked["origin"], checked["destination"])])
    checked = checked[(checked["slot"] >= start) & (checked["slot"] < end)]

    at = pd.MultiIndex.from_frame(checked[["origin", "allow_tolls", "slot"]])
    factor = conditions["factor"].reindex(at).fillna(1).to_numpy() if scale else 1
    stormy = conditions["stormy"].reindex(at).fillna(False).astype(bool).to_numpy()
    return pd.DataFrame({
        **base, "style": checked["source"].to_numpy(), "origin": checked["origin"].to_numpy(),
        "destination": checked["destination"].to_numpy(), "allow_tolls": checked["allow_tolls"].to_numpy(),
        "slot": checked["slot"].to_numpy(), "stormy": stormy, "actual_min": checked["travel_min"].to_numpy(),
        "median_min": checked["median"].to_numpy() * factor, "p90_min": checked["p90"].to_numpy() * factor,
    })


def minutes(value) -> str:
    return "–" if pd.isna(value) else f"{value:.0f} min"


def print_arrivals(checks: pd.DataFrame, rule: str, savings: bool):
    rows = []
    for (mode, tolls, style), group in checks[checks["rule"] == rule].groupby(["mode", "allow_tolls", "style"], sort=False):
        late = group.loc[group["late_min"] > 0, "late_min"]
        row = [mode, samples.toll_names[tolls], style, f"{len(group)}", f"{(group['late_min'] <= 0).mean():.0%}",
               minutes(-group["late_min"].median()), f"{late.max():.1f} min" if not late.empty else "–"]
        if savings:
            row += [minutes(group["saved_predicted_min"].median()), minutes(group["saved_actual_min"].median())]
        rows.append(row)

    header = ["Test", "Route", "Style", "Checks", "On time", "Early (median)", "Worst late"]
    if savings:
        header += ["Saves (advised)", "Saved (on the day)"]
    commute_stats.print_table(header, rows)


def print_waits(checks: pd.DataFrame):
    rows = []
    for (mode, tolls, style), group in checks[checks["rule"] == "wait to go home"].groupby(["mode", "allow_tolls", "style"], sort=False):
        saved = group["saved_actual_min"]
        rows.append([mode, samples.toll_names[tolls], style, f"{len(group)}",
                     minutes(group["saved_predicted_min"].median()), minutes(saved.median()),
                     f"{(saved >= commute_stats.worth_waiting_min).mean():.0%}", f"{saved.min():.0f} min"])
    commute_stats.print_table(["Test", "Route", "Style", "Waits", "Saves (advised)", "Saved (on the day)",
                               f"Saved {commute_stats.worth_waiting_min}+", "Least saved"], rows)


# The estimate at every slot, by hour of departure: share of drives under the advised p90
# and the median gap from the advised median (+ slower than advised)
def print_slots(slots: pd.DataFrame, modes: list[str]):
    slots = slots.assign(hour=slots["slot"] // 60, covered=slots["actual_min"] <= slots["p90_min"],
                         error=slots["actual_min"] - slots["median_min"])
    columns = [(mode, origin, destination) for origin, destination in samples.legs for mode in modes]
    rows = []
    for hour in sorted(slots["hour"].unique()):
        row = [f"{hour:02d}:00"]
        for mode, origin, destination in columns:
            cell = slots[(slots["hour"] == hour) & (slots["mode"] == mode) & (slots["origin"] == origin)]
            row.append(f"{cell['covered'].mean():.0%} · {cell['error'].median():+.1f}" if len(cell) else "–")
        rows.append(row)
    total = ["All"]
    for mode, origin, destination in columns:
        cell = slots[(slots["mode"] == mode) & (slots["origin"] == origin)]
        total.append(f"{cell['covered'].mean():.0%} · {cell['error'].median():+.1f}" if len(cell) else "–")
    commute_stats.print_table(["Leave", *(f"{samples.leg_name(o, d)} ({mode})" for mode, o, d in columns)], [*rows, total])


# Weather-blind against weather-aware advice, split by the held-out day's weather at the slot
def print_weather(checks: pd.DataFrame, slots: pd.DataFrame, modes: list[str]):
    leave_by = checks[checks["rule"] == "leave by"]
    rows = []
    for mode in modes:
        for stormy, label in [(False, "calm and dry"), (True, "rain, wet road or wind")]:
            def pick(frame, advice):
                return frame[(frame["mode"] == mode) & (frame["advice"] == advice) & (frame["stormy"] == stormy)]
            blind, aware = pick(leave_by, "weather-blind"), pick(leave_by, "weather-aware")
            blind_slots, aware_slots = pick(slots, "weather-blind"), pick(slots, "weather-aware")

            def on_time(frame):
                return f"{(frame['late_min'] <= 0).mean():.0%}" if len(frame) else "–"

            def covered(frame):
                return f"{(frame['actual_min'] <= frame['p90_min']).mean():.0%}" if len(frame) else "–"

            def worst(frame):
                late = frame.loc[frame["late_min"] > 0, "late_min"]
                return f"{late.max():.1f} min" if not late.empty else "–"

            rows.append([mode, label, f"{len(aware)}", f"{on_time(blind)} → {on_time(aware)}",
                         f"{worst(blind)} → {worst(aware)}", f"{len(aware_slots):,}",
                         f"{covered(blind_slots)} → {covered(aware_slots)}"])
    commute_stats.print_table(["Test", "Weather at the slot", "Leave-by checks", "On time: blind → aware",
                               "Worst late", "Slots", "Under p90: blind → aware"], rows)


# Each weather_model candidate against no weather effect, one weather day held out at a time:
# the median miss on that day's drives in rain, wet road or wind, as a share of the drive
def print_candidates(delays: pd.DataFrame):
    x = weather_model.terms(delays["rain_mm_h"], delays["wet_road_mm"], delays["gust_kmh"])
    dates, stormy = delays["departure_date"].to_numpy(), (x > 0).any(axis=1)
    days = sorted(np.unique(dates[stormy]))

    rows = []
    for name, fit in [("no weather effect", None), *weather_model.candidates.items()]:
        misses, sizes = [], []
        for day in days:
            test = (dates == day) & stormy
            held = delays[test]
            predicted = 0 if fit is None else fit(delays[dates != day]).factors(
                held["rain_mm_h"], held["wet_road_mm"], held["gust_kmh"]) - 1
            misses.append(np.median(np.abs(held["delay"].to_numpy() - predicted)))
            sizes.append(test.sum())
        rows.append([name, f"{np.average(misses, weights=sizes):.1%}", f"{max(misses):.1%}"])
    commute_stats.print_table(["Model", "Median miss", "Worst day"], rows)
    print(f"  ({len(days)} weather days, {stormy.sum():,} drives in rain, wet road or wind)")


# Held-out days with a late arrival, next to the day's weather and calendar
def print_misses(checks: pd.DataFrame, modes: list[str], weather_days: pd.DataFrame | None):
    arrivals = checks[checks["rule"].isin(["leave by", "leave earlier"])]
    missed = arrivals[arrivals["late_min"] > 0]
    if missed.empty:
        print("  No late arrivals.")
        return

    days = sorted(missed["day"].unique())
    context = features.calendar(days)
    if weather_days is not None:
        context = context.join(weather_days)

    rows = []
    for day in days:
        row = [day, pd.Timestamp(day).strftime("%a")]
        for mode in modes:
            tested = arrivals[(arrivals["mode"] == mode) & (arrivals["day"] == day)]
            late = tested["late_min"][tested["late_min"] > 0]
            row.append(f"{len(late)} of {len(tested)}" if len(tested) else "not tested")
        row.append(f"{missed.loc[missed['day'] == day, 'late_min'].max():.1f} min")
        if weather_days is not None:
            info = context.loc[day]
            row += [day_log.number(day_log.inches(info["rain_mm"]), ".2f"),
                    day_log.number(day_log.miles(info["max_gust_kmh"]), ".0f")]
        row.append(day_log.calendar_text(context.loc[day]))
        rows.append(row)

    header = ["Day", "", *(f"Late ({mode})" for mode in modes), "Worst"]
    if weather_days is not None:
        header += ["Rain in", "Gust mph"]
    commute_stats.print_table([*header, "Calendar"], rows)


def main():
    parser = argparse.ArgumentParser(description="Check the departure advice on days it didn't learn from.")
    samples.add_days_argument(parser)
    commute_stats.add_advice_arguments(parser)
    parser.add_argument("--mode", choices=["forward", "leave-one-out", "both"], default="both",
                        help="which other days each held-out day learns from (default both)")
    parser.add_argument("--min-days", type=int, default=default_min_days, metavar="N",
                        help=f"forward: earlier days a day needs before it's tested (default {default_min_days})")
    args = parser.parse_args()

    names = [style.name for style in driving_styles.styles if style in args.style]
    styled = driving_styles.style_samples(samples.load(args.days, google=True), samples.load_profiles())
    styled = styled[styled["source"].isin(names)]
    if styled.empty:
        sys.exit("No days with both Google and TomTom samples and a route profile yet; see commute_stats.py.")

    # Without weather data there's only weather-blind advice to check
    try:
        weather = weather_model.google_weather()
        delays = weather_model.with_delay(weather)
        _, _, readings = features.load()
        weather_days = features.daily_weather(readings)
    except SystemExit as err:
        print(f"Note: no weather data, so only weather-blind advice is checked ({err})")
        weather = delays = weather_days = None
    primary = "weather-aware" if weather is not None else "weather-blind"

    weekday_of = dict(zip(styled["departure_date"], styled["departure_weekday"]))
    dates = sorted(weekday_of)
    modes = ["forward", "leave-one-out"] if args.mode == "both" else [args.mode]

    rows, slot_frames, tested = [], [], {}
    for mode in modes:
        split = folds(dates, weekday_of, mode, args.min_days)
        tested[mode] = len(split)
        for test, train in split.items():
            history = styled[styled["departure_date"].isin(train)]
            day = styled[styled["departure_date"] == test]
            base = {"mode": mode, "day": test, "train_days": len(train)}

            stats = samples.slot_stats(history)
            variants = [("weather-blind", stats, day_weather(weather, test, None), False)]
            if weather is not None:
                others = delays["departure_date"] < test if mode == "forward" else delays["departure_date"] != test
                model = weather_model.fit(delays[others], runs=0)
                variants.append(("weather-aware", stats, day_weather(weather, test, model), True))

            for advice, stats, conditions, scale in variants:
                rows += check_day(stats, day, conditions, names, args, {**base, "advice": advice}, scale)
                slot_frames.append(check_slots(stats, day, conditions, {**base, "advice": advice}, scale))

    if not rows:
        sys.exit(f"Nothing to test yet: forward testing needs {args.min_days} earlier days in a group (--min-days).")
    checks = pd.DataFrame(rows)
    slots = pd.concat(slot_frames, ignore_index=True)
    main_checks, main_slots = checks[checks["advice"] == primary], slots[slots["advice"] == primary]

    print(f"{samples.days_label(args.days)} · {samples.coverage_label(styled)} · "
          + " · ".join(f"{mode}: {count} day{'s' if count != 1 else ''} tested" for mode, count in tested.items()))
    print("Each held-out day's advice comes from other days only, and is checked against that day's own drives: the")
    print("style's estimate from Google and TomTom, not a real arrival time.")
    if primary == "weather-aware":
        print("The advice is weather-aware, as commute_stats.py gives it with the weather given: the other days scaled")
        print("by the held-out day's own weather at each slot, as if the forecast had been right.")
    if checks["train_days"].min() < samples.trust_days:
        print(f"Early data: some advice came from fewer than {samples.trust_days} days, so its p90 is about the worst day seen.")

    print(f"\nLeave home by: on time if the held-out day's drive at the advised slot arrives {args.buffer} min before"
          f" class. A p90 rule aims for about 90%.\nEarly: minutes to spare before that.")
    print_arrivals(main_checks, "leave by", savings=False)

    print(f"\nLeave earlier (\"or\"): on time too, and the driving it saved on the held-out day vs the advice.")
    print_arrivals(main_checks, "leave earlier", savings=True)

    print(f"\nHeading home, when the advice is to wait: driving saved on the held-out day vs the advice.")
    print_waits(main_checks)

    print("\nEvery slot in the commute windows, all styles and routes: share of drives under the advised p90 (aim 90%)"
          " · median\nminutes slower (+) or faster (-) than the advised median.")
    print_slots(main_slots, modes)

    if weather is not None:
        print("\nWeather-blind vs weather-aware advice, by the held-out day's weather at the slot (rain falling, wet"
              f" road,\nor gusts past {weather_model.gust_start_mph:g} mph). The weather model is refitted on each"
              " held-out day's other days.")
        print_weather(checks, slots, modes)

        print("\nWeather models (weather_model.candidates), each fitted without one weather day and scored on it. A"
              " model\nreplaces the one in use only when it misses less here.")
        print_candidates(delays)

    print(f"\nLate arrivals by held-out day ({primary}; leave by and leave earlier):")
    print_misses(main_checks, modes, weather_days)

    samples.output_dir.mkdir(exist_ok=True)
    path = samples.output_dir / "backtest.csv"
    table = checks.assign(
        route=checks["allow_tolls"].map(samples.toll_names),
        time=checks["time"].map(samples.hhmm),
        slot=checks["slot"].map(samples.hhmm),
    )
    columns = ["mode", "advice", "rule", "day", "train_days", "style", "route", "time", "slot", "stormy",
               "predicted_min", "actual_min", "late_min", "saved_predicted_min", "saved_actual_min"]
    table.reindex(columns=columns).round(2).to_csv(path, index=False)

    slots_path = samples.output_dir / "backtest_slots.csv"
    slots.assign(
        direction=[samples.leg_name(o, d) for o, d in zip(slots["origin"], slots["destination"])],
        route=slots["allow_tolls"].map(samples.toll_names),
        slot=slots["slot"].map(samples.hhmm),
    )[["mode", "advice", "day", "train_days", "style", "direction", "route", "slot", "stormy",
       "actual_min", "median_min", "p90_min"]].round(2).to_csv(slots_path, index=False)
    print(f"\nEvery check: {path.relative_to(samples.repo_root)} · every slot: {slots_path.relative_to(samples.repo_root)}")


if __name__ == "__main__":
    main()

from dataclasses import dataclass

import argparse
import day_log
import features
import numpy as np
import pandas as pd

# The weather dimension of the drive-time estimate. A drive takes the dry-day baseline for
# its weekday group, departure slot and route, times (1 + effect), and the effect is fitted
# per drive from the weather along it:
#   raining:  rain falling at any route point as the car passes it
#   wet road: rain in the last few hours (features.wet_road_hours) but not falling now
#   wind:     mph of the strongest gust past gust_start_mph
#   heavy rain: mm/h past heavy_rain_mm_h, on top of raining; fitted only once heavy_min_days
#             days have had it, so light rain keeps the effect the data supports
# Neither service models weather, but both see the traffic slow in it. Google (primary) shows
# it clearly; TomTom, which leans on historical speeds, barely. So the fit uses Google samples
# from every collected day (the weekday is already in the baseline), with holidays left out.
# A scale factor rather than added minutes, so rain at rush hour costs more than at 03:00.
#
# Oct 2026 data: raining on the drive came out about 7% slow on each rainy day, whether 10% or
# 70% of the route was wet and at every rate seen. The dry hours of those days were 0%. All of
# it was light rain, under 0.2 in/h, so the heavy-rain term waits for heavier rain. The weights
# come from every drive; the ranges come from refitting on days drawn at random within their
# kind (rainy, windy, calm), and with a few rainy or windy days they're rough.

# Gusts below this don't slow the drive (dry Google drives: 0% under 30 mph, +3.5% at 30–40,
# +7.7% past 40, Oct 2026)
gust_start_mph: float = 25

# A weather term counts as rough until this many days had that weather
min_days: int = 8

# Refits on days drawn at random, for the ranges
bootstrap_runs: int = 300

km_per_mile: float = 1.609344
mm_per_inch: float = 25.4

# Rain past this rate (0.2 in/h) counts as heavy, and the heavy-rain term stays at 0 until
# this many days have had some. Everything sampled through Oct 2026 was lighter
heavy_rain_mm_h: float = 0.2 * mm_per_inch
heavy_min_days: int = 3

term_names = ["raining", "wet road", "wind", "heavy rain"]

# One Google drive per (route, day, slot), as driving_styles pairs them
pair_keys = ["origin", "destination", "allow_tolls", "departure_date", "slot"]


# The model's inputs for a forecast: the chance it rains on the drive (0–1), wet road for the
# rest of the time, mph of gust past gust_start_mph, and the chance times mm/h of rain past
# heavy_rain_mm_h. Works on arrays
def expected_terms(rain_chance, rain_mm_h, wet_road_mm, gust_kmh) -> np.ndarray:
    raining = np.clip(np.asarray(rain_chance, dtype=float), 0, 1)
    wet_road = (np.asarray(wet_road_mm, dtype=float) > 0) * (1 - raining)
    wind = np.clip(np.asarray(gust_kmh, dtype=float) / km_per_mile - gust_start_mph, 0, None)
    heavy = raining * np.clip(np.asarray(rain_mm_h, dtype=float) - heavy_rain_mm_h, 0, None)
    return np.stack([raining, wet_road, wind, heavy], axis=-1)


# The same for weather that happened: raining is 0 or 1
def terms(rain_mm_h, wet_road_mm, gust_kmh) -> np.ndarray:
    raining = (np.asarray(rain_mm_h, dtype=float) > 0).astype(float)
    return expected_terms(raining, rain_mm_h, wet_road_mm, gust_kmh)


@dataclass(frozen=True)
class WeatherModel:
    coef: np.ndarray            # one per term
    boots: np.ndarray           # the coefficients refitted on days drawn at random, one row each
    days: dict[str, list[str]]  # term -> days that had that weather
    drives: int

    # Drive-time multiplier for a drive's weather: rain falling (mm/h), rain in the last few
    # hours (mm), strongest gust (km/h). A forecast with rain implies a wet road too
    def factor(self, rain_mm_h: float = 0, wet_road_mm: float = 0, gust_kmh: float = 0) -> float:
        return float(self.factors(rain_mm_h, wet_road_mm, gust_kmh))

    # The same for arrays of drives
    def factors(self, rain_mm_h, wet_road_mm, gust_kmh) -> np.ndarray:
        return 1 + terms(rain_mm_h, wet_road_mm, gust_kmh) @ self.coef

    # For forecast weather, where rain is a chance (0–1): a 40% chance counts 0.4 of the rain weight
    def expected_factors(self, rain_chance, rain_mm_h, wet_road_mm, gust_kmh) -> np.ndarray:
        return 1 + expected_terms(rain_chance, rain_mm_h, wet_road_mm, gust_kmh) @ self.coef

    # The multiplier's 5th to 95th percentile over the refits
    def factor_range(self, rain_mm_h: float = 0, wet_road_mm: float = 0, gust_kmh: float = 0) -> tuple[float, float]:
        spread = 1 + self.boots @ terms(rain_mm_h, wet_road_mm, gust_kmh)
        return tuple(np.percentile(spread, [5, 95]))

    # Whether the heavy-rain term was fitted (heavy_min_days days had heavy rain)
    def heavy_active(self) -> bool:
        return len(self.days["heavy rain"]) >= heavy_min_days

    # "fitted on days with 3 raining, 3 wet road, 4 wind (rough until 8 each); heavy rain not
    # yet (0 of 3 days)"
    def basis(self) -> str:
        base = term_names[:3]
        counts = ", ".join(f"{len(self.days[name])} {name}" for name in base)
        text = f"fitted on days with {counts}"
        if min(len(self.days[name]) for name in base) < min_days:
            text += f" (rough until {min_days} each)"
        if self.heavy_active():
            return text + f"; heavy rain on {len(self.days['heavy rain'])} days"
        return text + f"; heavy rain not yet ({len(self.days['heavy rain'])} of {heavy_min_days} days)"


# Every Google drive with the weather along it (features.trip_weather), one row per pair_keys
def google_weather() -> pd.DataFrame:
    df, points, weather = features.load(google=True)
    df = df[df["source"] == "Google"]
    df = df.join(features.trip_weather(features.stops(df, features.stretches(points)), weather))
    return df.drop_duplicates(pair_keys)


# The drives to fit on: google_weather() rows with the dry-day baseline at their slot (median
# of calm dry drives: no rain, dry road, gusts under gust_start_mph). delay = drive time over
# that baseline, minus 1. Holidays and drives without a baseline or weather are left out
def with_delay(df: pd.DataFrame) -> pd.DataFrame:
    holidays = features.calendar(df["departure_date"].unique())["holiday"].astype(bool)
    df = df[~df["departure_date"].map(holidays)].copy()
    df["group"] = df["departure_weekday"].map(day_log.group_of)

    keys = ["origin", "allow_tolls", "group", "slot"]
    calm = (df["rain_mm_h"] == 0) & (df["wet_road_mm"] == 0) & (df["gust_kmh"] < gust_start_mph * km_per_mile)
    df = df.join(df[calm].groupby(keys)["travel_min"].median().rename("baseline_min"), on=keys)
    df["delay"] = df["travel_min"] / df["baseline_min"] - 1
    return df.dropna(subset=["delay", "rain_mm_h", "wet_road_mm", "gust_kmh"])


# Least squares on the terms in use; the others keep a weight of 0
def solve(x: np.ndarray, y: np.ndarray, used: np.ndarray) -> np.ndarray:
    coef = np.zeros(x.shape[1])
    coef[used] = np.linalg.lstsq(x[:, used], y, rcond=None)[0]
    return coef


# The weights come from every drive at once. runs refits give the ranges (0 skips them, e.g.
# for backtest.py's refit per held-out day)
def fit(df: pd.DataFrame, runs: int = bootstrap_runs) -> WeatherModel:
    x = terms(df["rain_mm_h"], df["wet_road_mm"], df["gust_kmh"])
    y = df["delay"].to_numpy()
    dates = df["departure_date"].to_numpy()
    days = {name: sorted(np.unique(dates[x[:, i] > 0])) for i, name in enumerate(term_names)}
    used = np.array([name != "heavy rain" or len(days[name]) >= heavy_min_days for name in term_names])
    coef = solve(x, y, used)

    # Days are the real unit: a day's drives share its traffic, so a refit draws whole days,
    # at random with replacement but within their kind (rain or wet road, else wind, else
    # calm), so every refit has as many rainy and windy days as the data
    rows_of = {day: np.flatnonzero(dates == day) for day in np.unique(dates)}
    kind = {day: "rain" if x[rows].max(axis=0)[[0, 1, 3]].any() else "wind" if x[rows, 2].max() > 0 else "calm"
            for day, rows in rows_of.items()}
    strata = [[day for day in rows_of if kind[day] == k] for k in ("rain", "wind", "calm")]
    rng = np.random.default_rng(0)
    boots = [coef]
    for _ in range(runs):
        picked = [day for stratum in strata if stratum for day in rng.choice(stratum, len(stratum))]
        rows = np.concatenate([rows_of[day] for day in picked])
        boots.append(solve(x[rows], y[rows], used))

    return WeatherModel(coef=coef, boots=np.array(boots), days=days, drives=len(df))


# Models that could replace fit(), each a fit function returning an object with factors()
# like WeatherModel's. backtest.py scores every one on weather days it didn't learn from; a
# flexible model (gradient boosting, quantile regression, a neural net) replaces fit() only
# once it wins there. With 6 weather days (Oct 2026), even 24 weights (each term by direction
# and part of day) missed by more than no weather effect at all
candidates = {"3 weights": lambda df: fit(df, runs=0)}


# A slot_stats series (indexed by slot) with its median, p10 and p90 times factor: one number
# for every slot, or a Series by slot (slots without one keep theirs)
def scale(series: pd.DataFrame, factor) -> pd.DataFrame:
    if isinstance(factor, pd.Series):
        factor = factor.reindex(series.index).fillna(1)
    return series.assign(**{column: series[column] * factor for column in ("median", "p10", "p90")})


# The forecast weather a drive would meet from each origin at every slot of a local date
# (features.planned_trips with typical drive times, each point's block as the car passes it)
# and its multiplier. Rain is the highest chance along the drive; wet road and gusts come
# from the forecast's own rain and wind (features.trip_weather). Indexed by (origin, slot):
# factor, rain_chance (0–1), gust_kmh, fetched_at. Slots the forecast doesn't reach are left out
def forecast_factors(day, model: WeatherModel, typical: dict[str, pd.Series], forecast: pd.DataFrame,
                     points: pd.DataFrame) -> pd.DataFrame:
    line = features.stretches(points)
    frames = []
    for origin, typical_min in typical.items():
        trips = features.stops(features.planned_trips(day, origin, typical_min), line)
        passed = trips.assign(time=trips["passed_at"].dt.ceil("15min")).merge(
            forecast[["point_id", "time", "precip_prob_pct", "fetched_at"]], on=["point_id", "time"], how="left"
        )
        by_trip = passed.groupby("sample").agg(chance=("precip_prob_pct", "max"), fetched_at=("fetched_at", "max"))
        weather = features.trip_weather(trips, forecast).join(by_trip)
        weather = weather[weather["precip_mm"].notna()]

        # A block without a chance falls back on whether the forecast has rain in it
        chance = (weather["chance"] / 100).fillna((weather["rain_mm_h"] > 0).astype(float))
        frames.append(pd.DataFrame({
            "origin": origin, "slot": weather.index,
            "factor": model.expected_factors(chance, weather["rain_mm_h"], weather["wet_road_mm"], weather["gust_kmh"]),
            "rain_chance": chance.to_numpy(), "gust_kmh": weather["gust_kmh"].to_numpy(),
            "fetched_at": weather["fetched_at"].to_numpy(),
        }))
    return pd.concat(frames, ignore_index=True).set_index(["origin", "slot"]) if frames else pd.DataFrame()


def main():
    argparse.ArgumentParser(description="Fit how rain and wind slow the drive, from the samples so far.").parse_args()
    df = with_delay(google_weather())
    model = fit(df)

    print(f"Weather effect on drive times: Google, every collected day but holidays · {model.drives:,} drives,"
          f" {model.basis()}")
    print("Drive time = dry-day baseline for the weekday group, slot and route × (1 + effect).")
    for name in term_names:
        print(f"  {name.capitalize()}: {', '.join(model.days[name]) or 'no days yet'}")

    print("\nMultiplier, with the range from refitting on days drawn at random within their kind (5th–95th"
          " percentile),\nand minutes added to a 60-minute dry drive:")
    scenarios = [
        ("Raining on the drive", dict(rain_mm_h=1, wet_road_mm=1)),
        ("Heavy rain, 0.5 in/h", dict(rain_mm_h=0.5 * mm_per_inch, wet_road_mm=1)),
        ("Wet road, rain stopped", dict(wet_road_mm=1)),
        *((f"Gusts {mph} mph, dry", dict(gust_kmh=mph * km_per_mile)) for mph in (30, 40, 50)),
        ("Raining, gusts 40 mph", dict(rain_mm_h=1, wet_road_mm=1, gust_kmh=40 * km_per_mile)),
    ]
    rows = []
    for label, weather in scenarios:
        f, (lo, hi) = model.factor(**weather), model.factor_range(**weather)
        rows.append([label, f"×{f:.3f}", f"×{lo:.3f}–{hi:.3f}", f"{(f - 1) * 60:+.1f} min"])
    day_log.print_table(["Weather", "Multiplier", "Range", "On 60 min"], rows)

    # How the fit lines up with the measured delays, by condition
    df = df.assign(model=terms(df["rain_mm_h"], df["wet_road_mm"], df["gust_kmh"]) @ model.coef)
    gust_mph = df["gust_kmh"] / km_per_mile
    conditions = [
        ("Raining on the drive", df["rain_mm_h"] > 0),
        ("Wet road, rain stopped", (df["rain_mm_h"] == 0) & (df["wet_road_mm"] > 0)),
        ("Dry, gusts 25–40 mph", (df["wet_road_mm"] == 0) & gust_mph.between(gust_start_mph, 40, inclusive="left")),
        ("Dry, gusts 40+ mph", (df["wet_road_mm"] == 0) & (gust_mph >= 40)),
        ("Calm and dry", (df["wet_road_mm"] == 0) & (df["rain_mm_h"] == 0) & (gust_mph < gust_start_mph)),
    ]
    print("\nMeasured delay (median) vs the model, by condition:")
    rows = []
    for label, mask in conditions:
        group = df[mask]
        if group.empty:
            continue
        rows.append([label, f"{len(group):,}", f"{group['departure_date'].nunique()}",
                     f"{group['delay'].median():+.1%}", f"{group['model'].median():+.1%}"])
    day_log.print_table(["Condition", "Drives", "Days", "Measured", "Model"], rows)


if __name__ == "__main__":
    main()

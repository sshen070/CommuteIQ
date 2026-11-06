from core import features, samples
from dataclasses import dataclass, field
from datetime import date, timedelta
from models import driving_styles, weather_model

import argparse
import pandas as pd

# The departure advice shared by commute_stats.py (tables), backtest.py (checks on held-out
# days) and daily_advice.py (the nightly Telegram message): each driving style's per-slot drive
# times scaled to the weather (basis), and the rules that pick a departure from them (leave_by,
# leave_earlier, heading_home), plus the command-line options they share. Nothing here prints.

# How far before class to look for a departure slot
lookback_min: int = 4 * 60

# How long you'd stay at uni to dodge traffic, and the least drive time worth waiting for,
# before class (leaving home earlier) or after it. When waiting is worth it, the departure
# is the one closest to the no-wait slot whose median is within near_best_min of the best
# in the window and still saves worth_waiting_min. A slot's median is uncertain by about
# 1-2 min with a few weeks of days, so near_best_min stays well above that; each extra
# minute of it trades about 6 min less waiting for 1 min more driving
max_wait_min: int = 3 * 60
worth_waiting_min: int = 10
near_best_min: int = 5

# Class times checked when none are given (commute_stats.py, backtest.py)
default_class_starts = list(range(8 * 60, 12 * 60 + 1, 30))   # 08:00, 08:30, ... 12:00
default_class_ends = list(range(12 * 60, 18 * 60 + 1, 60))    # 12:00, 13:00, ... 18:00


# The data can't give advice yet (no route profile, or no days with both sources); says why
class NoAdvice(Exception):
    pass


# What the advice rests on: samples, style times and their per-slot statistics, and the
# weather multiplier per origin (one number for a what-if, a Series by slot for a forecast)
@dataclass
class Basis:
    df: pd.DataFrame            # samples.load(days, google=True)
    profiles: pd.DataFrame      # samples.load_profiles()
    styled: pd.DataFrame        # driving_styles.style_samples
    stats: pd.DataFrame         # samples.slot_stats(styled)
    weather_note: str           # where the multipliers came from, for a header line
    factors: dict = field(default_factory=dict)
    forecast: pd.DataFrame | None = None    # weather_model.forecast_factors, when the forecast was used

    # One leg, toll option and style, indexed by slot, scaled to the weather
    def series(self, origin: str, destination: str, tolls: int, style: str) -> pd.DataFrame:
        return weather_model.scale(series_for(self.stats, origin, destination, tolls, style), self.factors.get(origin, 1))


# The drives as sampled (nearly all dry and calm so far), scaled per slot to the weather:
# a what-if (rain, wet road, gusts), which holds for every departure, or else the stored
# forecast for day (None = as sampled). Taking each drive's own weather out first was
# tested and lost the margin the few weather days give the p90 (see CLAUDE.md)
def basis(days: list[str], day: date | None, rain_in_h: float = 0, wet_road: bool = False,
          gust_mph: float = 0) -> Basis:
    df = samples.load(days, google=True)
    profiles = samples.load_profiles()
    routes = df[driving_styles.route_keys].drop_duplicates()
    if missing := len(routes.merge(profiles, on=driving_styles.route_keys, how="left").query("freeway_km.isna()")):
        raise NoAdvice(f"{missing} route{'s have' if missing != 1 else ' has'} no freeway profile yet. Run python"
                       " route_profile.py from backend/app (see its header), or restart an updated collector.")
    styled = driving_styles.style_samples(df, profiles)
    if styled.empty:
        raise NoAdvice(f"No {samples.days_label(days)} days with both Google and TomTom samples yet, so driving"
                       " styles can't be computed. Add GOOGLE_CLOUD_API_KEY to .env and let the collector run.")
    result = Basis(df, profiles, styled, samples.slot_stats(styled), "as sampled, without the forecast")

    asked = [f"rain {rain_in_h:g} in/h" if rain_in_h else "", "wet road" if wet_road and not rain_in_h else "",
             f"gusts {gust_mph:g} mph" if gust_mph else ""]
    if asked := ", ".join(part for part in asked if part):
        model = weather_model.fit(weather_model.with_delay(weather_model.google_weather()))
        conditions = dict(rain_mm_h=rain_in_h * weather_model.mm_per_inch,
                          wet_road_mm=1 if rain_in_h or wet_road else 0,
                          gust_kmh=gust_mph * weather_model.km_per_mile)
        factor = model.factor(**conditions)
        low, high = model.factor_range(**conditions)
        result.factors = {"HOME": factor, "UNI": factor}
        result.weather_note = f"{asked}: every drive ×{factor:.3f} (×{low:.3f}–{high:.3f}), {model.basis()}"
    elif day is None:
        pass
    elif (stored := features.load_forecast())[0].empty:
        result.weather_note = "as sampled: no forecast stored yet (python -m conditions.forecast in backend/app)"
    else:
        model = weather_model.fit(weather_model.with_delay(weather_model.google_weather()))
        google = df[(df["source"] == "Google") & (df["allow_tolls"] == 0)]
        typical = {origin: group.groupby("slot")["travel_min"].median() for origin, group in google.groupby("origin")}
        factors = weather_model.forecast_factors(day, model, typical, *stored)
        if factors.empty:
            result.weather_note = f"as sampled: the stored forecast doesn't reach {day}"
        else:
            result.factors = {origin: factors.loc[origin, "factor"] for origin in factors.index.get_level_values("origin").unique()}
            result.forecast = factors
            result.weather_note = forecast_note(day, factors, model)

    return result


# One origin's forecast rows inside its commute window, indexed by slot
def in_window(factors: pd.DataFrame, origin: str, destination: str) -> pd.DataFrame:
    if origin not in factors.index.get_level_values("origin"):
        return factors.iloc[:0].droplevel("origin")
    start, end = samples.window(origin, destination)
    leg = factors.loc[origin]
    return leg[(leg.index >= start) & (leg.index < end)]


# "forecast for Tue Oct 27 (updated Mon 15:04): Home → Uni rain chance up to 40%, ...", over
# each direction's commute window
def forecast_note(day: date, factors: pd.DataFrame, model) -> str:
    parts = []
    for origin, destination in samples.legs:
        if not (leg := in_window(factors, origin, destination)).empty:
            parts.append(f"{samples.leg_name(origin, destination)} rain chance up to {leg['rain_chance'].max():.0%},"
                         f" gusts up to {leg['gust_kmh'].max() / weather_model.km_per_mile:.0f} mph,"
                         f" drives up to ×{leg['factor'].max():.3f}")
    updated = pd.Timestamp(factors["fetched_at"].max()).strftime("%a %H:%M")
    return f"forecast for {day:%a %b} {day.day} (updated {updated}): {'; '.join(parts)}; model {model.basis()}"


# One leg, toll option and source or style, indexed by slot (minutes after midnight)
def series_for(stats: pd.DataFrame, origin: str, destination: str, tolls: int, source: str) -> pd.DataFrame:
    rows = stats[
        (stats["source"] == source) & (stats["origin"] == origin) & (stats["destination"] == destination)
        & (stats["allow_tolls"] == tolls)
    ]
    return rows.set_index("slot").sort_index()


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


# Leave home earlier than the latest safe slot: the latest slot in the lookback window
# whose median is within near_best_min of the window's best and at least worth_waiting_min
# shorter, so the least time is spent waiting at uni. Every slot before the latest safe
# one also arrives on time (see leave_by)
def leave_earlier(series: pd.DataFrame, latest: pd.Series, class_start: int) -> pd.Series | None:
    window = series[(series.index >= class_start - lookback_min) & (series.index < latest.name)]
    worth_it = window[(window["median"] <= window["median"].min() + near_best_min)
                      & (window["median"] <= latest["median"] - worth_waiting_min)]
    return worth_it.iloc[-1] if not worth_it.empty else None


# Leave right after class, or wait for the first slot (within max_wait_min) whose median
# is within near_best_min of the best and at least worth_waiting_min shorter
def heading_home(series: pd.DataFrame, class_end: int) -> tuple[pd.Series, pd.Series] | None:
    # "Leave then" must be the class-end slot itself, not the next slot that has data
    first_slot = class_end // samples.slot_min * samples.slot_min
    if first_slot not in series.index:
        return None

    window = series[(series.index >= first_slot) & (series.index <= class_end + max_wait_min)]

    right_away = window.iloc[0]
    worth_it = window[(window["median"] <= window["median"].min() + near_best_min)
                      & (window["median"] <= right_away["median"] - worth_waiting_min)]

    # Not worth staying unless it saves real time
    best = worth_it.iloc[0] if not worth_it.empty else right_away
    return right_away, best


# argparse type: "aggressive", "Defensive", "rel" -> that driving style
def parse_style(text: str) -> driving_styles.Style:
    matches = [style for style in driving_styles.styles if style.name.lower().startswith(text.strip().lower())]
    if len(matches) != 1:
        names = ", ".join(style.name.lower() for style in driving_styles.styles)
        raise argparse.ArgumentTypeError(f"unknown driving style {text!r}; use {names}")
    return matches[0]


# argparse type: "today", "tomorrow" or YYYY-MM-DD -> date
def parse_date(text: str) -> date:
    text = text.strip().lower()
    if text in ("today", "tomorrow"):
        return date.today() + timedelta(days=text == "tomorrow")
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected today, tomorrow or YYYY-MM-DD (got {text!r})")


# Class times, buffer and styles: commute_stats.py's, and backtest.py's, which checks the same advice
def add_advice_arguments(parser: argparse.ArgumentParser):
    parser.add_argument("--class-start", type=samples.parse_hhmm, nargs="+", default=default_class_starts,
                        metavar="HH:MM", help="first class start time(s); default 08:00 to 12:00 every 30 min")
    parser.add_argument("--class-end", type=samples.parse_hhmm, nargs="+", default=default_class_ends,
                        metavar="HH:MM", help="last class end time(s); default 12:00 to 18:00 hourly")
    parser.add_argument("--buffer", type=int, default=15, metavar="MIN",
                        help="minutes to park and walk to class (default 15)")
    parser.add_argument("--style", type=parse_style, nargs="+", default=driving_styles.styles, metavar="STYLE",
                        help="driving style(s) to show: aggressive, defensive, relaxed (default: all)")

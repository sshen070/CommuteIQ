from core import calendar_days, samples
from datetime import date, datetime, timedelta
from dotenv import load_dotenv
from models import advice, weather_model

import argparse
import os
import pandas as pd
import re
import requests
import sys

# One Telegram message with a day's commute advice for one driving style: when to leave home
# for the first class (and an earlier option when it saves real driving), and whether to wait
# at uni before driving home, with the stored forecast: commute_stats.py's numbers for that
# day. deploy/commuteiq-advice.timer runs it at 21:10, after the collector's 21:05 forecast
# refresh. Run from analysis/:
#   python -m reports.daily_advice --dry-run          # print the message instead of sending it
#   python -m reports.daily_advice --date 2026-11-02  # a given day (default: tomorrow, today before noon)
# From .env: CLASS_SCHEDULE, CLASS_BUFFER_MIN, DRIVING_STYLE, TELEGRAM_BOT_TOKEN and
# TELEGRAM_CHAT_ID. A weekday missing from the schedule, or a day calendar.csv marks without
# classes (closures, breaks, finals), gets no message. This is the one outside call in
# analysis/: a message to your own chat, which uses no API allowance.

telegram_url: str = "https://api.telegram.org/bot{token}/sendMessage"
timeout: int = 10

default_buffer_min: int = 15
default_style: str = "defensive"

toward = {"HOME": "to uni", "UNI": "home"}


# Before noon the advice is for today: a run the timer caught up on after the server was off
# at 21:10. Otherwise tomorrow
def default_day(now: datetime) -> date:
    return now.date() if now.hour < 12 else now.date() + timedelta(days=1)


def label(day: date) -> str:
    return f"{day:%a %b} {day.day}"


# CLASS_SCHEDULE, e.g. "mon-thu 09:00-15:20; fri 10:00-11:50" -> {weekday: (first class start,
# last class end)} in minutes after midnight
def schedule(text: str) -> dict[str, tuple[int, int]]:
    classes = {}
    for part in filter(None, (part.strip() for part in re.split(r"[;\n]", text))):
        days, _, times = part.rpartition(" ")
        start, _, end = times.partition("-")
        try:
            weekdays, first, last = samples.parse_days(days), samples.parse_hhmm(start), samples.parse_hhmm(end)
        except argparse.ArgumentTypeError as err:
            sys.exit(f"CLASS_SCHEDULE: can't read {part!r} ({err}). Write e.g. mon-thu 09:00-15:20; fri 10:00-11:50")
        if last <= first:
            sys.exit(f"CLASS_SCHEDULE: {part!r} ends before it starts")
        classes.update({day: (first, last) for day in weekdays})
    return classes


def env_int(name: str, default: int) -> int:
    value = (os.getenv(name) or "").strip()
    if value and not value.isdigit():
        sys.exit(f"{name} must be whole minutes (got {value!r})")
    return int(value) if value else default


# "Forecast (updated 21:05): rain chance up to 40% to uni, 10% home; gusts to 22 mph; ...",
# over each direction's commute window
def weather_line(basis: advice.Basis) -> str:
    if basis.forecast is None:
        return f"Weather: {basis.weather_note}."
    legs = {origin: advice.in_window(basis.forecast, origin, destination) for origin, destination in samples.legs}
    legs = {origin: leg for origin, leg in legs.items() if not leg.empty}
    if not legs:
        return "Forecast: none inside the commute windows."

    rain = ", ".join(f"{leg['rain_chance'].max():.0%} {toward[origin]}" for origin, leg in legs.items())
    gust = max(leg["gust_kmh"].max() for leg in legs.values()) / weather_model.km_per_mile
    slower = max(leg["factor"].max() for leg in legs.values()) - 1
    updated = pd.Timestamp(basis.forecast["fetched_at"].max()).strftime("%H:%M")
    line = f"Forecast (updated {updated}): rain chance up to {rain}; gusts to {gust:.0f} mph"
    return line + (f"; drives up to {slower:.0%} slower." if slower >= 0.005 else ".")


def message(day: date, start: int, end: int, buffer: int, style, basis: advice.Basis) -> str:
    hhmm = samples.hhmm
    lines = [f"{label(day)} · class {hhmm(start)}–{hhmm(end)} · {style.name.lower()} driving", "",
             f"To uni, parked by {hhmm(start - buffer)}:"]

    for tolls in sorted(basis.styled[basis.styled["origin"] == "HOME"]["allow_tolls"].unique()):
        route = samples.toll_names[tolls]
        series = basis.series("HOME", "UNI", tolls, style.name)
        latest = advice.leave_by(series, start, buffer)
        if isinstance(latest, str):
            lines.append(f"  {route}: {latest if latest == 'no data' else 'leave ' + latest}")
            continue
        lines.append(f"  {route}: leave by {hhmm(latest.name)} ({latest['p90']:.0f} min on a bad day,"
                     f" {latest['median']:.0f} typical)")
        if (earlier := advice.leave_earlier(series, latest, start)) is not None:
            lines.append(f"    or at {hhmm(earlier.name)}: {earlier['median']:.0f} min typical")

    lines += ["", f"Home after {hhmm(end)}:"]
    for tolls in sorted(basis.styled[basis.styled["origin"] == "UNI"]["allow_tolls"].unique()):
        route = samples.toll_names[tolls]
        if (result := advice.heading_home(basis.series("UNI", "HOME", tolls, style.name), end)) is None:
            lines.append(f"  {route}: no data")
            continue
        now, best = result
        if best.name == now.name:
            lines.append(f"  {route}: right away ({now['median']:.0f} min, home about"
                         f" {hhmm(max(now.name, end) + now['median'])})")
        else:
            lines.append(f"  {route}: wait until {hhmm(best.name)} ({best['median']:.0f} min instead of"
                         f" {now['median']:.0f}, home about {hhmm(best.name + best['median'])})")

    lines += ["", weather_line(basis)]
    if (most_days := int(basis.stats["days"].max())) < samples.trust_days:
        lines.append(f"Early data: at most {most_days} days per slot, so \"bad day\" is about the worst day seen.")
    return "\n".join(lines)


# The message for a day, or None (with the reason printed) when there's no class
def advice_text(day: date, now: datetime) -> str | None:
    if not (classes := schedule(os.getenv("CLASS_SCHEDULE") or "")):
        sys.exit("Set CLASS_SCHEDULE in .env, e.g. mon-thu 09:00-15:20; fri 10:00-11:50 (see .env.example)")
    if (weekday := f"{day:%A}") not in classes:
        print(f"No class on {weekday}s in CLASS_SCHEDULE: no message.")
        return None
    if (status := calendar_days.tags(day, calendar_days.load())["uni"]) != "classes":
        print(f"UCR {status} on {label(day)} (calendar.csv): no message.")
        return None

    start, end = classes[weekday]
    buffer = env_int("CLASS_BUFFER_MIN", default_buffer_min)
    if day == now.date() and now.hour * 60 + now.minute >= start - buffer:
        print(f"Too late for {label(day)}'s {samples.hhmm(start)} class: no message.")
        return None
    try:
        style = advice.parse_style(os.getenv("DRIVING_STYLE") or default_style)
    except argparse.ArgumentTypeError as err:
        sys.exit(f"DRIVING_STYLE: {err}")

    # The day's own group (Mon–Thu, Fri, ...), as commute_stats.py --days would pool it
    group = next(group for group in samples.day_groups if weekday in group)
    return message(day, start, end, buffer, style, advice.basis(group, day))


def send(text: str):
    token, chat = (os.getenv(name, "").strip() for name in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"))
    if not token or not chat:
        sys.exit("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env (see .env.example), or use --dry-run")

    # The token is part of the URL, which requests' errors quote, so only the error's kind is shown
    try:
        response = requests.post(telegram_url.format(token=token), json={"chat_id": chat, "text": text}, timeout=timeout)
    except requests.RequestException as err:
        sys.exit(f"Telegram sendMessage failed: {type(err).__name__}")
    if response.status_code != 200:
        sys.exit(f"Telegram sendMessage failed: HTTP {response.status_code} {' '.join(response.text.split())[:200]}")


def main():
    parser = argparse.ArgumentParser(description="A day's leave-by times, as one Telegram message.")
    parser.add_argument("--date", type=advice.parse_date, metavar="DAY",
                        help="today, tomorrow or YYYY-MM-DD (default: tomorrow, or today before noon)")
    parser.add_argument("--dry-run", action="store_true", help="print the message instead of sending it")
    args = parser.parse_args()
    load_dotenv(samples.repo_root / ".env")

    now = datetime.now()
    day = args.date or default_day(now)
    deliver = print if args.dry_run else send

    # Whatever stops the advice, say so on the phone too: a silent evening looks like "no class"
    try:
        text = advice_text(day, now)
    except advice.NoAdvice as err:
        text = f"{label(day)}: no advice yet. {err}"
    except SystemExit as err:
        if not isinstance(err.code, str):
            raise
        text = f"{label(day)}: no advice. {err.code}"
    except Exception as err:
        deliver(f"{label(day)}: the advice failed ({type(err).__name__}). See journalctl -u commuteiq-advice.")
        raise

    if text is not None:
        deliver(text)


if __name__ == "__main__":
    main()

# CommuteIQ

What's the latest I can leave home and still make my first class? CommuteIQ answers that from data instead of guesswork. A collector running 24/7 on a Raspberry Pi 5 logs live drive times between home and campus every 5 minutes, in both directions, from Google Maps and TomTom, along with the weather along the route. Analysis scripts turn weeks of those samples into a latest safe departure time for each class start and a "leave now or wait it out" call for the drive home, for three driving styles.

## Results so far

From 11 Monday–Thursday class days with samples from both services (Oct 5 – Oct 22, 2026). Every number is recomputed from the database on each run, so they firm up as more weeks come in. The driving styles rest on assumed speeds and weights that haven't been checked against real drives yet.

| Finding | Result |
|---|---|
| Latest safe departure for a 9:00 class (bad-day p90, 15 min to park and walk) | Toll-free: **07:35** aggressive, **07:30** defensive, **07:30** relaxed. With tolls: **07:45**, **07:40**, **07:40** |
| Driving style matters off-peak, not at rush hour | For an 11:00 class the three styles leave **10 min** apart (09:55 to 09:45); for a 9:00 class, 5 min |
| Morning drive to campus, by departure time (Google, toll-free) | **26 min swing**: 47 min at 05:00 vs 72 min at 07:45 (median) |
| Waiting out the evening peak (defensive, toll-free) | Leaving campus at 19:00 instead of 17:00: **19 min less driving** (67 → 48 min median) |
| Toll route vs toll-free, inside the commute windows (Google) | Saves a median **7 min** to campus and **8 min** home (up to 28 and 17 min) |
| Google vs TomTom, matched by day and departure time | Google reads a median **3 min** higher, and 7–13 min higher 1 time in 10 |
| Holiday effect (Columbus Day / Indigenous Peoples' Day) | 3–4 min faster than an average Mon–Thu in both directions |
| Rain (5 light rainy days so far) | No slowdown yet. The drive home met a headwind on 4 of the 5 days |

## How it works

```
Google Routes ──┐
TomTom Routing ─┼─> collector (every 5 min) ─> SQLite ─> analysis ─> leave-by tables per driving style, charts, day log
Open-Meteo ─────┘
```

**Collector** (`backend/app/`)

- On each clock-aligned 5-minute slot, asks Google Routes (best-guess estimate) and TomTom for the drive time with live traffic, from home to campus and back, toll-free and with toll roads allowed.
- Samples inside configurable commute windows (05:00–14:00 toward campus, 12:00–21:00 toward home), plus "baseline weeks" sampled around the clock for a full-day picture. Google samples on weekdays.
- Measures each route once from TomTom at 03:00: how many miles are freeway and how long the freeway part takes on an open road.
- Every 15 minutes, records weather at 16 fixed points spaced 5 km apart along the route. Past weather is backfilled from Open-Meteo's archive.
- Built to run unattended:
  - A failed request is logged and skipped, never fatal.
  - If the clock jumps, a slot is skipped rather than sampled late.
  - After a power cut, it waits for NTP before sampling.
  - It tracks each API's free-tier budget and Google trial credits, and won't exceed them.
  - It redacts API keys from logs.

**Analysis** (`analysis/`)

- Buckets samples by direction, route (toll-free / tolls), day group (Mon–Thu, Fri, Sat, Sun) and 5-minute departure slot, and takes the median, p10 and p90 of each bucket.
- **Driving styles:** aggressive (85 mph, weaves through lanes), defensive (75 mph in the fast lane, follows traffic) and relaxed (65 mph, any lane). Each style's drive time starts from Google's estimate for the same day and slot, moves toward TomTom's lower estimate for drivers who weave, and adds the time a style's cruising speed saves or costs on the freeway.
- **Congestion, not on or off:** how much freedom traffic leaves to drive your own speed. It runs from the freeway's open-road speed (0) down to 35 mph, Caltrans' threshold for congestion (1). Style differences shrink as traffic builds and vanish in stop-and-go.
- **Leave-by time:** the latest slot whose p90 drive still arrives a set buffer before class, provided every earlier slot in the 4 hours before class also makes it. That way one lucky late slot isn't recommended over an unlucky earlier one.
- **Heading home:** leave right after class, unless waiting up to 3 hours cuts the median drive by at least 10 minutes.
- **Day log:** compares each day with the average of the other days in its group at the same departure times. It lines that up with rain, length of the dry spell before it, visibility, wind, heat, holidays and the academic calendar, plus a rain-and-wind table per direction.
- **Per-drive features:** the weather at each route point at the moment the car would pass it, including headwind and crosswind gusts where it was raining, and sun glare (km of road heading into a low sun, from NOAA solar equations). These are exported as CSVs for later modeling.

## APIs

| API | Used for | Status |
|---|---|---|
| Google Routes (`computeRoutes`, `BEST_GUESS`) | The primary drive-time estimate, toll-free and with tolls | Weekdays since Oct 5, 2026, around the clock Oct 5–11 and Nov 2–8 |
| TomTom Routing | A second live-traffic drive time, a lower bound in traffic; each route's freeway profile | Since Sep 30, 2026 |
| TomTom Search | Place search and reverse geocoding during setup | In use |
| Google Places (New) | Setup's place search when a Google key is set; it finds places TomTom doesn't index, such as individual parking lots | Optional |
| Open-Meteo Forecast + Historical Forecast | Weather at 16 points along the route, live and archived | In use, no key needed |

So far: about 17,500 TomTom and 11,700 Google drive-time samples, and 236,000 weather readings.

## Stack

- Python 3 (3.14 in the container), `requests`, `python-dotenv`
- SQLite through the standard-library `sqlite3`, no ORM
- pandas and matplotlib for analysis
- Docker Compose, started at boot by a systemd unit, on a Raspberry Pi 5

There's no web server yet. The collector is a long-running loop and the analysis is a set of command-line scripts. FastAPI is listed in `backend/requirements.txt` for a planned API.

## Running it

You need a TomTom API key (the free tier covers the default windows). A Google Cloud key is optional, but the driving styles need Google's samples.

On a Linux server, with Docker:

```sh
cp .env.example .env                                            # set TOMTOM_API_KEY (and GOOGLE_CLOUD_API_KEY)
sudo docker compose build
sudo docker compose run --rm collector python setup_locations.py   # pick home and campus
sudo docker compose run --rm collector python -m conditions.backfill_weather
sudo cp deploy/commuteiq.service /etc/systemd/system/           # edit WorkingDirectory first
sudo systemctl daemon-reload && sudo systemctl enable --now commuteiq
journalctl -u commuteiq -f
```

Locally, without Docker:

```sh
pip install -r backend/requirements.txt
cd backend/app
python setup_locations.py
python main.py
```

Analysis (it reads the database read-only, so it's safe while the collector runs):

```sh
pip install -r analysis/requirements.txt
cd analysis
python commute_stats.py      # leave-by times; --class-start 09:30 --class-end 15:20 --buffer 15 --style defensive
python plot_drive_times.py   # one chart per direction, in analysis/output/
python day_log.py            # one line per day: weather, calendar, how it compared
```

`.env.example` documents every setting: sampling windows, baseline weeks, toll routes, weather interval and API budgets.

## Layout

```
backend/app/              collector: main loop, TomTom/Google clients, route profiles, SQLite schema, setup
backend/app/conditions/   weather points along the route, Open-Meteo client, backfill
analysis/                 shared loader, driving styles, leave-by stats, charts, day log, features, calendar.csv
deploy/                   systemd unit
Dockerfile, docker-compose.yml
```

## Status

- **Phase 1, reliable collector:** done.
- **Phase 2, collecting class weeks:** in progress, with a second baseline week Nov 2–8.
- **Phase 3, leave-by recommendations:** the scripts are in place. The p90s mean more once each slot has 10+ days of data, and the scripts warn until then. Next: test the leave-by rule on days held out of the data, and log real drives to check the driving styles.

Home and campus locations stay in `.env` and the database, both gitignored, as does analysis output.

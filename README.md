# CommuteIQ

What's the latest I can leave home and still make my first class? CommuteIQ answers that from data instead of guesswork. A collector running 24/7 on a Raspberry Pi 5 logs live drive times between home and campus every 5 minutes, in both directions, along with the weather along the route. Analysis scripts turn weeks of those samples into a latest safe departure time for each class start, and a "leave now or wait it out" call for the drive home.

## Results so far

From 11 Monday–Thursday class days (Sep 30 – Oct 19, 2026; 9,069 TomTom samples). Every number is recomputed from the database on each run, so they firm up as more weeks come in.

| Finding | Result |
|---|---|
| Waiting out the evening peak: leaving campus at 18:50 instead of 17:00 | **13 min less driving** (64 → 52 min median) |
| Morning drive to campus, by departure time (toll-free) | **18 min swing**: 46 min at 05:00 vs 63 min at 08:00 (median) |
| Toll route vs toll-free, inside the commute windows | Saves a median **7 min** to campus and **10 min** home (up to 15 min at 17:20) |
| Latest safe departure for a 9:00 class (bad-day p90, 15 min to park and walk) | **07:40** toll-free, **07:45** with tolls |
| Holiday effect (Columbus Day / Indigenous Peoples' Day) | 3–4 min faster than an average Mon–Thu in both directions |

## How it works

```
TomTom Routing ─┐
Google Routes ──┼─> collector (every 5 min) ─> SQLite ─> analysis ─> leave-by tables, charts, day log
Open-Meteo ─────┘
```

**Collector** (`backend/app/`)

- On each clock-aligned 5-minute slot, requests the fastest route with live traffic from home to campus and back, toll-free and optionally with toll roads allowed.
- Samples inside configurable commute windows (05:00–14:00 toward campus, 12:00–21:00 toward home), plus "baseline weeks" sampled around the clock for a full-day picture.
- Every 15 minutes, records weather at 16 fixed points spaced 5 km apart along the route. Past weather is backfilled from Open-Meteo's archive.
- Built to run unattended. A failed request is logged and skipped, never fatal. A slot is skipped rather than sampled late if the clock jumps. After a power cut, it waits for NTP before sampling. It tracks each API's free-tier budget and won't exceed it, and it redacts API keys from logs.

**Analysis** (`analysis/`)

- Buckets samples by direction, route (toll-free / tolls), day group (Mon–Thu, Fri, Sat, Sun) and 5-minute departure slot, and takes the median, p10 and p90 of each bucket.
- **Leave-by time:** the latest slot whose p90 drive still arrives a set buffer before class, provided every earlier slot in the 4 hours before class also makes it. That way one lucky late slot isn't recommended over an unlucky earlier one.
- **Heading home:** leave right after class, unless waiting up to 3 hours cuts the median drive by at least 10 minutes.
- **Day log:** compares each day with the average of the other days in its group at the same departure times. It lines that up with rain, length of the dry spell before it, visibility, wind, heat, holidays and the academic calendar.
- **Per-drive features:** the weather at each route point at the moment the car would pass it, and sun glare (km of road heading into a low sun, from NOAA solar equations). These are exported as CSVs for later modeling.

## APIs

| API | Used for | Status |
|---|---|---|
| TomTom Routing | Live-traffic drive times, the primary data | Collected since Sep 30, 2026; all analysis is based on it |
| TomTom Search | Place search and reverse geocoding during setup | In use |
| Google Routes (`computeRoutes`, `BEST_GUESS`) | An independent drive-time estimate, toll-free and with tolls | Collected on weekdays since Oct 5, 2026; not yet in the analysis |
| Google Places (New) | Setup's place search when a Google key is set; it finds places TomTom doesn't index, such as individual parking lots | Optional |
| Open-Meteo Forecast + Historical Forecast | Weather at 16 points along the route, live and archived | In use, no key needed |

So far: about 16,400 TomTom and 10,900 Google drive-time samples.

## Stack

- Python 3 (3.14 in the container), `requests`, `python-dotenv`
- SQLite through the standard-library `sqlite3`, no ORM
- pandas and matplotlib for analysis
- Docker Compose, started at boot by a systemd unit, on a Raspberry Pi 5

There's no web server yet. The collector is a long-running loop and the analysis is a set of command-line scripts. FastAPI is listed in `backend/requirements.txt` for a planned API.

## Running it

You need a TomTom API key (the free tier covers the default windows). A Google Cloud key is optional.

On a Linux server, with Docker:

```sh
cp .env.example .env                                            # set TOMTOM_API_KEY
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
python commute_stats.py      # leave-by times; --class-start 09:30 --class-end 15:20 --buffer 15
python plot_drive_times.py   # charts per direction and weekday, in analysis/output/
python day_log.py            # one line per day: weather, calendar, how it compared
```

`.env.example` documents every setting: sampling windows, baseline weeks, toll routes, weather interval and API budgets.

## Layout

```
backend/app/        collector: main loop, TomTom/Google clients, SQLite schema, setup
backend/app/conditions/   weather points along the route, Open-Meteo client, backfill
analysis/           shared loader, leave-by stats, charts, day log, features, calendar.csv
deploy/             systemd unit
Dockerfile, docker-compose.yml
```

## Status

- **Phase 1, reliable collector:** done.
- **Phase 2, collecting 3–4 weeks of class days:** in progress.
- **Phase 3, leave-by recommendations:** the scripts are in place. The p90s mean more once each slot has 10+ days of data, and the scripts warn until then.

Home and campus locations stay in `.env` and the database, both gitignored, as does analysis output.

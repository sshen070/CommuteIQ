# Weather along the route: a factor besides traffic that may change a drive. Kept apart
# from the drive-time collection in main.py, which only calls WeatherCollector. Run
# modules from backend/app, e.g. python -m conditions.backfill_weather. The calendar
# (holidays, UCR terms) isn't collected, so it lives with the analysis

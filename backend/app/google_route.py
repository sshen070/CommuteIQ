from dataclasses import dataclass, field
from datetime import datetime

@dataclass
class GoogleRoute:

    # Raw API values; Google writes durations as strings like "1534s"
    duration: str = field(repr=False)           # with traffic, under trafficModel
    staticDuration: str = field(repr=False)     # same route ignoring traffic
    distanceMeters: int = field(repr=False)

    # Request values: Google returns no timestamps, so departure is when the request was sent
    trafficModel: str = field(repr=False)       # e.g. "PESSIMISTIC"
    departureTime: str = field(repr=False)      # local ISO-8601 with UTC offset

    # Derived values
    travel_time_s: int = field(init=False)
    no_traffic_time_s: int = field(init=False)
    travel_time_min: float = field(init=False)

    departure_date: str = field(init=False)
    departure_weekday: str = field(init=False)
    departure_time: str = field(init=False)

    def __post_init__(self):

        # "1534s" -> 1534
        self.travel_time_s = round(float(self.duration.removesuffix("s")))
        self.no_traffic_time_s = round(float(self.staticDuration.removesuffix("s")))
        self.travel_time_min = round(self.travel_time_s / 60, 2)

        # Same split as TripRoute, so both tables filter the same way
        dep = datetime.fromisoformat(self.departureTime)

        self.departure_date = dep.strftime("%Y-%m-%d")
        self.departure_weekday = dep.strftime("%A")
        self.departure_time = dep.strftime("%H:%M")

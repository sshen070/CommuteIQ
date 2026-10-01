from dataclasses import dataclass, field
from datetime import datetime

@dataclass
class TripRoute:
    
    # Raw API values
    lengthInMeters: int = field(repr=False)
    travelTimeInSeconds: int = field(repr=False)
    trafficDelayInSeconds: int = field(repr=False)
    trafficLengthInMeters: int = field(repr=False) 
    departureTime: str = field(repr=False)
    arrivalTime: str = field(repr=False)

    # Derived values
    distance_miles: float = field(init=False)
    traffic_length_miles: float = field(init=False)    

    travel_time_min: float = field(init=False)
    traffic_delay_min: float = field(init=False)

    departure_date: str = field(init=False)
    departure_weekday: str = field(init=False)
    departure_time: str = field(init=False)
    departure_time_zone: str = field(init=False)

    arrival_date: str = field(init=False)
    arrival_time: str = field(init=False)
    arrival_time_zone: str = field(init=False)

    def __post_init__(self):

        # Metric -> USC
        meters_to_miles: float = 1609.34
        min_to_sec: int = 60

        self.distance_miles = round(self.lengthInMeters / meters_to_miles, 2)
        self.traffic_length_miles = round(self.trafficLengthInMeters / meters_to_miles, 2)

        self.travel_time_min = round(self.travelTimeInSeconds / min_to_sec, 2)
        self.traffic_delay_min = round(self.trafficDelayInSeconds / min_to_sec, 2)

        # Parse timestamps (TomTom reports local time at the origin)
        dep = datetime.fromisoformat(self.departureTime)
        arr = datetime.fromisoformat(self.arrivalTime)

        # 24-hour HH:MM sorts and compares as text, e.g. BETWEEN '07:00' AND '09:00'
        self.departure_date = dep.strftime("%Y-%m-%d")
        self.departure_weekday = dep.strftime("%A")
        self.departure_time = dep.strftime("%H:%M")
        self.departure_time_zone = str(dep.tzinfo)

        self.arrival_date = arr.strftime("%Y-%m-%d")
        self.arrival_time = arr.strftime("%H:%M")
        self.arrival_time_zone = str(arr.tzinfo)
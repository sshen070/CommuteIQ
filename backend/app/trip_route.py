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

        # Parse timestamps
        dep = datetime.fromisoformat(self.departureTime)
        arr = datetime.fromisoformat(self.arrivalTime)

        self.departure_date = dep.strftime("%Y-%m-%d")
        self.departure_time = dep.strftime("%H:%M:%S")
        self.departure_time_zone = str(dep.tzinfo)

        self.arrival_date = arr.strftime("%Y-%m-%d")
        self.arrival_time = arr.strftime("%H:%M:%S")
        self.arrival_time_zone = str(arr.tzinfo)
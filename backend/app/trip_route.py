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
    traffic_delay_miles: float = field(init=False)    

    travel_time_min: float = field(init=False)
    traffic_delay_min: float = field(init=False)

    d_date: str = field(init=False)
    d_time: str = field(init=False)
    d_time_zone: str = field(init=False)

    a_date: str = field(init=False)
    a_time: str = field(init=False)
    a_time_zone: str = field(init=False)

    def __post_init__(self):

        # Metric -> USC
        meters_to_miles: float = 1609.34
        min_to_sec: int = 60

        self.distance_miles = self.lengthInMeters / meters_to_miles
        self.traffic_delay_miles = self.trafficLengthInMeters / meters_to_miles

        self.travel_time_min = self.travelTimeInSeconds / min_to_sec
        self.traffic_delay_min = self.trafficDelayInSeconds / min_to_sec

        # Parse timestamps
        dep = datetime.fromisoformat(self.departureTime)
        arr = datetime.fromisoformat(self.arrivalTime)

        self.d_date = dep.strftime("%Y-%m-%d")
        self.d_time = dep.strftime("%H:%M:%S")
        self.d_time_zone = str(dep.tzinfo)

        self.a_date = arr.strftime("%Y-%m-%d")
        self.a_time = arr.strftime("%H:%M:%S")
        self.a_time_zone = str(arr.tzinfo)
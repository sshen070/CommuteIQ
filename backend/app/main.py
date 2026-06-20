from data_extraction import return_url
from database import *
from trip_route import TripRoute


def main():
    
    # UCI --> UCR
    x1, y1, x2, y2 = ("33.643250", "-117.838606", "33.969535", "-117.332683")
    print(f'Coords #1: {x1}, {y1}\nCoords #2: {x2}, {y2}\n')

    while True:
        
        # Run query (fetch updated values)
        route_data: dict = return_url(x1, y1, x2, y2, os.getenv("TOMTOM_API_KEY"))
        
        route: TripRoute = TripRoute(
            lengthInMeters=route_data["lengthInMeters"],
            travelTimeInSeconds=route_data["travelTimeInSeconds"],
            trafficDelayInSeconds=route_data["trafficDelayInSeconds"],
            trafficLengthInMeters=route_data["trafficLengthInMeters"],
            departureTime=route_data["departureTime"],
            arrivalTime=route_data["arrivalTime"]
        )
        
        print(route)

        # Add route stats to database table
        run_extraction(route)

if __name__ == "__main__":
    main()


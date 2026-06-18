from data_extraction import return_url
from trip_route import TripRoute

# url = "https://api.tomtom.com/routing/1/calculateRoute/52.50931,13.42936:52.50274,13.43872/json?vehicleHeading=90&sectionType=traffic&report=effectiveSettings&routeType=eco&traffic=true&avoid=unpavedRoads&travelMode=car&vehicleMaxSpeed=120&vehicleCommercial=false&vehicleEngineType=combustion&key=2cljWfCfDZqNG3egY6vWQzLOWc44S1pr"
# print(requests.get(url))

def main():
    api_key = "2cljWfCfDZqNG3egY6vWQzLOWc44S1pr"
    
    # UCI --> UCR
    x1, y1, x2, y2 = ("33.643250", "-117.838606", "33.969535", "-117.332683")
    print(f'Coords #1: {x1}, {y1}\nCoords #2: {x2}, {y2}\n')

    route_data = return_url(x1, y1, x2, y2, api_key)
    # print(route_data)
    
    route = TripRoute(
        lengthInMeters=route_data["lengthInMeters"],
        travelTimeInSeconds=route_data["travelTimeInSeconds"],
        trafficDelayInSeconds=route_data["trafficDelayInSeconds"],
        trafficLengthInMeters=route_data["trafficLengthInMeters"],
        departureTime=route_data["departureTime"],
        arrivalTime=route_data["arrivalTime"]
    )

    print(route)

if __name__ == "__main__":
    main()


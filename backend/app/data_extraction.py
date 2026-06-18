import requests

base_url: str = "https://api.tomtom.com/routing/1/calculateRoute/"
    
def return_url(x1: str, y1: str, x2: str, y2: str, api_key: str) -> str:
    
    # Create base url
    coords: str = f'{x1},{y1}:{x2},{y2}' 
    base_path: str = f"{base_url}{coords}/json"

    # Create query ~ determine optimal path with no tollRoads
    params = {
        "key": api_key,
        "traffic": "true",
        "travelMode": "car",
        "routeType": "fastest",

        # Avoid paid roads
        "avoid": "tollRoads",
    }

    # Make POST API call (response ~ request status)
    response = requests.get(base_path, params=params)

    if response.status_code == 200:
        route_data = response.json()

        routes = "routes"
        summary = "summary"

        # Print route stats
        if routes in route_data:
            if summary in route_data[routes][0]:
                for entries in route_data[routes][0][summary]:
                    print(f"{entries}: {route_data[routes][0][summary][entries]}")

        route_stats = route_data[routes][0][summary]
        return route_stats

    else:
       print(f"Request failed! {response}")

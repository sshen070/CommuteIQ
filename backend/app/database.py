from trip_route import TripRoute

import os
import time
import psycopg2

# Load variables from .env file
from dotenv import load_dotenv

load_dotenv()

# Create table if does not already exist
def create_table(conn):

    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS commute_samples (
        id SERIAL PRIMARY KEY,

        origin VARCHAR(100) NOT NULL,
        destination VARCHAR(100) NOT NULL,

        distance_miles DOUBLE PRECISION NOT NULL,
        traffic_length_miles DOUBLE PRECISION NOT NULL,
        travel_time_min DOUBLE PRECISION NOT NULL,
        traffic_delay_min DOUBLE PRECISION NOT NULL,

        departure_time VARCHAR(100) NOT NULL,
        departure_date VARCHAR(100) NOT NULL,

        arrival_time VARCHAR(100) NOT NULL,
        arrival_date VARCHAR(100) NOT NULL,
                
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)

    conn.commit()
    cur.close()


def add_route(route: TripRoute, conn):
    cur = conn.cursor()

    cur.execute(
        """
        INSERT INTO commute_samples
        (
            origin,
            destination,
            distance_miles,
            traffic_length_miles,
            travel_time_min,
            traffic_delay_min,
            departure_time,
            departure_date,
            arrival_time,
            arrival_date
        )
        VALUES
        (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            "UCI",
            "UCR",
            route.distance_miles,
            route.traffic_length_miles,
            route.travel_time_min,
            route.traffic_delay_min,
            route.departure_time,
            route.departure_date,
            route.arrival_time,
            route.arrival_date
        )
    )
    conn.commit()

def run_extraction(route: TripRoute):
    conn = psycopg2.connect(
        host = os.getenv("DB_HOST"),
        database = os.getenv("DB_NAME"),
        user = os.getenv("DB_USER"),
        password = os.getenv("DB_PASSWORD")
    )

    # If does not already exist
    create_table(conn)

    # Add route to table & 5 min delay
    add_route(route, conn)
    time.sleep(300)

    conn.close()
from dataclasses import dataclass
from datetime import datetime
from location import Location

# Table and queries for real drives. CREATE TABLE IF NOT EXISTS adds it to an existing
# database without touching the other tables. Times are naive local datetimes in Python and
# local ISO-8601 with UTC offset in the table, as everywhere else.


@dataclass
class Drive:
    id: int
    origin: str                 # role, "HOME" or "UNI"
    destination: str
    departure: datetime
    arrival: datetime | None
    allow_tolls: int | None
    had_stop: int
    typed: int
    created: datetime           # when it was logged, which for a typed drive can be days later


def create_tables(conn):
    conn.executescript("""
    -- Real drives, logged by tapping the Telegram bot's buttons when you leave and when you
    -- arrive (drives/bot.py). Times are Telegram's message timestamps, or times typed with
    -- /adddrive. A clean drive has an arrival, a route and no stop; the rest are left out of
    -- the style check (analysis/validation/real_drives.py)
    CREATE TABLE IF NOT EXISTS real_drives (
        id INTEGER PRIMARY KEY,

        origin_id INTEGER NOT NULL REFERENCES locations(id),
        destination_id INTEGER NOT NULL REFERENCES locations(id),

        departure_at TEXT NOT NULL,
        departure_date TEXT NOT NULL,       -- local split, as in the sample tables
        departure_weekday TEXT NOT NULL,
        departure_time TEXT NOT NULL,       -- HH:MM to the minute, not a 5-minute slot

        arrival_at TEXT,                    -- NULL until the arrival is tapped
        travel_time_s INTEGER,

        -- NULL until answered after arriving: 1 = toll roads, 0 = toll-free
        allow_tolls INTEGER CHECK (allow_tolls IN (0, 1)),

        -- 1 = stopped on the way (gas, coffee), so the drive time isn't a drive
        had_stop INTEGER NOT NULL DEFAULT 0 CHECK (had_stop IN (0, 1)),

        -- 1 = times typed with /adddrive (to the minute, from memory) rather than tapped
        typed INTEGER NOT NULL DEFAULT 0 CHECK (typed IN (0, 1)),

        created_at TEXT NOT NULL
    );
    """)

    # Added Nov 5, 2026, after the table was first made: CREATE TABLE IF NOT EXISTS leaves
    # an existing table as it is
    if "typed" not in [row[1] for row in conn.execute("PRAGMA table_info(real_drives)")]:
        conn.execute("ALTER TABLE real_drives ADD COLUMN typed INTEGER NOT NULL DEFAULT 0 CHECK (typed IN (0, 1))")
        conn.commit()


def stamp(at: datetime) -> str:
    return at.astimezone().isoformat(timespec="seconds")


def parse(text: str | None) -> datetime | None:
    return datetime.fromisoformat(text).astimezone().replace(tzinfo=None) if text else None


select = """
    SELECT d.id, o.role, t.role, d.departure_at, d.arrival_at, d.allow_tolls, d.had_stop, d.typed, d.created_at
    FROM real_drives d
    JOIN locations o ON o.id = d.origin_id
    JOIN locations t ON t.id = d.destination_id
"""


def to_drive(row) -> Drive:
    return Drive(row[0], row[1], row[2], parse(row[3]), parse(row[4]), row[5], row[6], row[7], parse(row[8]))


def get(conn, drive_id: int) -> Drive | None:
    row = conn.execute(f"{select} WHERE d.id = ?", (drive_id,)).fetchone()
    return to_drive(row) if row else None


# The most recently logged drives, newest first
def latest(conn, count: int = 1) -> list[Drive]:
    return [to_drive(row) for row in conn.execute(f"{select} ORDER BY d.id DESC LIMIT ?", (count,))]


# Drives that set off on any of these local dates (YYYY-MM-DD)
def on_dates(conn, dates: list[str]) -> list[Drive]:
    marks = ", ".join("?" * len(dates))
    return [to_drive(row) for row in conn.execute(f"{select} WHERE d.departure_date IN ({marks})", dates)]


# A tapped departure (arrival to come), or a whole typed drive
def add_drive(conn, origin: Location, destination: Location, departure: datetime, arrival: datetime | None = None) -> int:
    drive_id = conn.execute(
        """
        INSERT INTO real_drives (origin_id, destination_id, departure_at, departure_date, departure_weekday,
                                 departure_time, arrival_at, travel_time_s, typed, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (origin.id, destination.id, stamp(departure), f"{departure:%Y-%m-%d}", f"{departure:%A}", f"{departure:%H:%M}",
         stamp(arrival) if arrival else None, round((arrival - departure).total_seconds()) if arrival else None,
         int(arrival is not None), stamp(datetime.now())),
    ).lastrowid
    conn.commit()
    return drive_id


# Typed times replacing a drive's own (an open drive completed, or a finished one corrected)
def set_times(conn, drive_id: int, departure: datetime, arrival: datetime):
    conn.execute(
        """
        UPDATE real_drives SET departure_at = ?, departure_date = ?, departure_weekday = ?, departure_time = ?,
                               arrival_at = ?, travel_time_s = ?, typed = 1
        WHERE id = ?
        """,
        (stamp(departure), f"{departure:%Y-%m-%d}", f"{departure:%A}", f"{departure:%H:%M}", stamp(arrival),
         round((arrival - departure).total_seconds()), drive_id),
    )
    conn.commit()


def set_departure(conn, drive_id: int, at: datetime):
    conn.execute(
        "UPDATE real_drives SET departure_at = ?, departure_date = ?, departure_weekday = ?, departure_time = ? WHERE id = ?",
        (stamp(at), f"{at:%Y-%m-%d}", f"{at:%A}", f"{at:%H:%M}", drive_id),
    )
    conn.commit()


# None clears the arrival, and the route answer with it
def set_arrival(conn, drive: Drive, at: datetime | None):
    if at is None:
        conn.execute("UPDATE real_drives SET arrival_at = NULL, travel_time_s = NULL, allow_tolls = NULL, had_stop = 0"
                     " WHERE id = ?", (drive.id,))
    else:
        conn.execute("UPDATE real_drives SET arrival_at = ?, travel_time_s = ? WHERE id = ?",
                     (stamp(at), round((at - drive.departure).total_seconds()), drive.id))
    conn.commit()


def set_route(conn, drive_id: int, allow_tolls: int | None, had_stop: bool):
    conn.execute("UPDATE real_drives SET allow_tolls = ?, had_stop = ? WHERE id = ?", (allow_tolls, int(had_stop), drive_id))
    conn.commit()


def delete(conn, drive_id: int):
    conn.execute("DELETE FROM real_drives WHERE id = ?", (drive_id,))
    conn.commit()

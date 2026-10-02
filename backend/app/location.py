from dataclasses import dataclass

@dataclass
class Location:

    # Resolved via Google or TomTom search
    address: str
    lat: float
    lon: float
    name: str | None = None     # place name, e.g. "UCR Lot 41"

    # Set once saved to the database
    role: str | None = None     # "HOME" or "UNI"
    id: int | None = None

    def __str__(self):
        return f"{self.name} - {self.address}" if self.name else self.address

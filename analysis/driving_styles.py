from dataclasses import dataclass

import pandas as pd

# Drive time per driving style, built from Google (primary) and TomTom samples matched by
# day and 5-minute slot, and each route's freeway km and open-road freeway time
# (samples.load_profiles). Two things move a style away from Google's number:
#   - Speed: on an open road drivers pick their own speed, so cruising above or below the
#     traffic changes the freeway part of the drive. Dense traffic takes that control away.
#   - Weaving: TomTom reads lower than Google once traffic builds. Drivers who work the
#     lanes come closer to TomTom; in heavy traffic everyone sits in the same queue and
#     lands near Google, give or take a couple of minutes.
# A congestion level per sample slides each style between its open-road and heavy-traffic
# behavior, since traffic isn't on or off. It comes from how fast freeway traffic moved in
# that sample: 0 at the freeway's open-road speed, 1 at congested_mph.


@dataclass(frozen=True)
class Style:
    name: str
    description: str
    cruise_mph: float       # speed on an open freeway
    tomtom_open: float      # TomTom's weight in the blend on an open road (Google gets the rest)
    tomtom_heavy: float     # TomTom's weight in heavy traffic


styles = [
    Style("Aggressive", "80+ mph, weaves through lanes", 85, 0.75, 0.25),
    Style("Defensive", "65–75 mph in the fast lane, follows traffic", 75, 0.40, 0.15),
    Style("Relaxed", "60–65 mph in any lane", 65, 0.10, 0.05),
]

# Average freeway speed at which traffic counts as congested: Caltrans' threshold for
# congestion on its freeways, where flow is mostly stop-and-go and lanes move together
# (congestion 1). Between that and the open-road speed, gaps still open up for drivers who
# keep up or change lanes. The freeway's capacity speed (about 53 mph, Highway Capacity
# Manual) would put "no room left" too early: speed is averaged over the whole route, and
# a busy road averaging that speed still has gaps
congested_mph: float = 35

# Open-road time per direction and route: this low percentile of Google's door-to-door times
open_road_quantile: float = 0.1

km_per_mile: float = 1.609344

route_keys = ["origin", "destination", "allow_tolls"]
pair_keys = [*route_keys, "departure_date", "departure_weekday", "slot"]


# Per (leg, toll option, day, slot) with both a Google and a TomTom sample, one row per style:
# source = style name, travel_min, congestion, and the route's open-road freeway speed
# (open_mph). Days with only one source, and routes without a profile, are dropped
def style_samples(df: pd.DataFrame, profiles: pd.DataFrame) -> pd.DataFrame:
    by_source = df.groupby([*pair_keys, "source"])["travel_min"].mean().unstack("source")
    if not {"Google", "TomTom"} <= set(by_source.columns):
        return pd.DataFrame(columns=[*pair_keys, "source", "travel_min", "congestion", "open_mph"])

    paired = by_source.dropna(subset=["Google", "TomTom"]).reset_index().merge(profiles, on=route_keys)
    open_road = paired.groupby(route_keys)["Google"].transform(lambda s: s.quantile(open_road_quantile))

    # Surface streets keep their open-road time; whatever Google adds beyond that is freeway traffic
    freeway_mi = paired["freeway_km"] / km_per_mile
    freeway_open_min = paired["freeway_share"] * open_road
    open_mph = freeway_mi / freeway_open_min * 60
    traffic_mph = freeway_mi / (paired["Google"] - (open_road - freeway_open_min)) * 60
    congestion = ((open_mph - traffic_mph) / (open_mph - congested_mph)).clip(0, 1)

    frames = []
    for style in styles:
        weight = style.tomtom_open + (style.tomtom_heavy - style.tomtom_open) * congestion
        blend = weight * paired["TomTom"] + (1 - weight) * paired["Google"]
        # Minutes gained or lost by cruising the freeway at the style's speed instead of the
        # open-road traffic's, fading out as traffic closes the gaps
        speed = (freeway_mi / style.cruise_mph * 60 - freeway_open_min) * (1 - congestion)
        frames.append(paired[pair_keys].assign(
            source=style.name, travel_min=blend + speed, congestion=congestion, open_mph=open_mph,
        ))

    return pd.concat(frames, ignore_index=True)

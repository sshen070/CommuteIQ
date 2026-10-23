from matplotlib import patheffects
from matplotlib.lines import Line2D
from matplotlib.patches import Patch
from pathlib import Path

import argparse
import matplotlib
import pandas as pd
import samples

matplotlib.use("Agg")   # headless: the server has no display
import matplotlib.pyplot as plt

# One chart per direction: drive time by local departure time, toll-free and
# toll routes together. Google's best guess (primary) is a solid median with its p10–p90
# band; TomTom is a dashed median with its samples, in the same route color.
# Writes output/home_to_uni.png and output/uni_to_home.png.

# Fixed per toll option, so a chart with only one option keeps the same color
series_colors = {0: "#2a78d6", 1: "#eb6834"}
tomtom_dashes = (0, (4, 2.5))

surface = "#fcfcfb"
ink = "#0b0b0b"
ink_secondary = "#52514e"
ink_muted = "#898781"
gridline = "#e1e0d9"
baseline = "#c3c2b7"


def plot_leg(df: pd.DataFrame, stats: pd.DataFrame, origin: str, destination: str, days: list[str]) -> Path | None:
    leg = df[(df["origin"] == origin) & (df["destination"] == destination)]
    if leg.empty:
        return None

    fig, ax = plt.subplots(figsize=(12, 6.5), dpi=150)
    fig.patch.set_facecolor(surface)
    ax.set_facecolor(surface)

    tomtom = leg[leg["source"] == "TomTom"]
    toll_options = sorted(tomtom["allow_tolls"].unique())
    google_options = sorted(leg[leg["source"] == "Google"]["allow_tolls"].unique())
    peaks = []

    # Reindex to every slot so collector downtime shows as a gap, not a straight line
    def slot_series(source: str, tolls: int) -> pd.DataFrame:
        return stats[
            (stats["source"] == source) & (stats["origin"] == origin) & (stats["destination"] == destination)
            & (stats["allow_tolls"] == tolls)
        ].set_index("slot").reindex(range(0, 24 * 60, samples.slot_min))

    x = (pd.RangeIndex(0, 24 * 60, samples.slot_min) + samples.slot_min / 2) / 60

    # TomTom: every sample, faintly, so day-to-day spread stays visible, and a dashed median
    for tolls in toll_options:
        color = series_colors[tolls]
        raw = tomtom[tomtom["allow_tolls"] == tolls]
        ax.scatter(raw["dep_min"] / 60, raw["travel_min"], s=6, color=color, alpha=0.3, linewidths=0, zorder=2)
        ax.plot(x, slot_series("TomTom", tolls)["median"], color=color, linewidth=1.6, linestyle=tomtom_dashes,
                dash_capstyle="round", zorder=3)

    # Google (primary): band and solid median
    for tolls in google_options:
        color = series_colors[tolls]
        series = slot_series("Google", tolls)
        ax.fill_between(x, series["p10"], series["p90"], color=color, alpha=0.12, linewidth=0, zorder=1)
        ax.plot(x, series["median"], color=color, linewidth=2, solid_joinstyle="round", solid_capstyle="round", zorder=3)

    # Each route's peak, on Google's line where it was sampled, else TomTom's
    for tolls in sorted(set(toll_options) | set(google_options)):
        series = slot_series("Google" if tolls in google_options else "TomTom", tolls)
        peak_slot = series["median"].idxmax()
        peaks.append(((peak_slot + samples.slot_min / 2) / 60, series.at[peak_slot, "median"], peak_slot,
                      series_colors[tolls]))

    top = leg["travel_min"].max() * 1.12
    ax.set_ylim(0, top)
    ax.set_xlim(0, 24)

    # Label each series' worst slot; if two peak labels would collide, put the lower one underneath
    peaks.sort(key=lambda peak: peak[1], reverse=True)
    for i, (px, py, slot, color) in enumerate(peaks):
        below = i > 0 and abs(px - peaks[0][0]) < 2.5 and abs(py - peaks[0][1]) < top * 0.1
        ax.plot(px, py, "o", markersize=8, color=color, markeredgecolor=surface, markeredgewidth=2, zorder=4)
        ax.annotate(
            f"Peak {samples.hhmm(slot)} · {py:.0f} min",
            (px, py), xytext=(0, -16 if below else 10), textcoords="offset points",
            ha="center", va="top" if below else "bottom", fontsize=9, color=ink_secondary, zorder=5,
            path_effects=[patheffects.withStroke(linewidth=3, foreground=surface)],
        )

    # Axes: hairline grid, recessive chrome
    ax.set_xticks(range(0, 25, 2), [f"{hour:02d}:00" for hour in range(0, 25, 2)])
    ax.set_xticks(range(25), minor=True)
    ax.grid(axis="y", color=gridline, linewidth=0.8)
    ax.grid(axis="x", which="major", color=gridline, linewidth=0.8)
    ax.set_axisbelow(True)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(baseline)
    ax.tick_params(colors=ink_muted, labelsize=9, length=0)
    ax.tick_params(axis="x", which="minor", length=3, color=baseline)
    ax.set_xlabel("Departure time (local)", color=ink_secondary, fontsize=10)
    ax.set_ylabel("Drive time (min)", color=ink_secondary, fontsize=10)

    # Legend above the plot, where peaks and labels can never land on it: Google in the
    # first row, TomTom in a second under the matching route. Blanks hold the grid in place
    def blank() -> Line2D:
        return Line2D([], [], linestyle="", label="")

    routes = sorted(set(toll_options) | set(google_options))
    google_row = [
        Line2D([], [], color=series_colors[tolls], linewidth=2, label=f"{samples.toll_names[tolls]} · Google median")
        if tolls in google_options else blank()
        for tolls in routes
    ]
    google_row.append(Patch(color=ink_muted, alpha=0.25, linewidth=0, label="Middle 80% of Google samples (p10–p90)"))
    tomtom_row = [
        Line2D([], [], color=series_colors[tolls], linewidth=1.6, linestyle=tomtom_dashes,
               label=f"{samples.toll_names[tolls]} · TomTom median")
        if tolls in toll_options else blank()
        for tolls in routes
    ]
    tomtom_row.append(
        Line2D([], [], linestyle="", marker="o", markersize=4, color=ink_muted, alpha=0.6, label="One TomTom sample")
    )
    if google_options:
        # Legends fill column by column, so interleave the two rows
        handles = [handle for column in zip(google_row, tomtom_row) for handle in column]
    else:
        handles = tomtom_row
    ax.legend(handles=handles, loc="lower left", bbox_to_anchor=(0, 1.01), ncol=len(tomtom_row), borderaxespad=0,
              frameon=False, fontsize=9, labelcolor=ink_secondary, handlelength=1.8, columnspacing=1.8)
    legend_lift = 0.045 if google_options else 0

    # Title and subtitle, left-aligned above the legend
    subtitle = f"{samples.days_label(days)} · {samples.coverage_label(leg)} · {samples.slot_min}-minute departure slots"
    if (days_per_slot := leg.groupby("slot")["departure_date"].nunique().max()) < samples.trust_days:
        subtitle += f"\nEarly data ({days_per_slot} day{'s' if days_per_slot != 1 else ''} per slot): lines and bands will shift as more days are collected"
    subtitle_lines = subtitle.count("\n") + 1
    ax.text(0, 1.08 + legend_lift, subtitle, transform=ax.transAxes, fontsize=10, color=ink_secondary, va="bottom", linespacing=1.4)
    ax.text(0, 1.095 + 0.042 * subtitle_lines + legend_lift, f"{samples.leg_name(origin, destination)}: drive time by departure time",
            transform=ax.transAxes, fontsize=15, fontweight="bold", color=ink, va="bottom")

    samples.output_dir.mkdir(exist_ok=True)
    path = samples.output_dir / f"{origin.lower()}_to_{destination.lower()}.png"
    fig.savefig(path, facecolor=surface, bbox_inches="tight")
    plt.close(fig)
    return path


def main():
    parser = argparse.ArgumentParser(description="Plot drive time by departure time, one chart per direction.")
    samples.add_days_argument(parser)
    args = parser.parse_args()

    df = samples.load(args.days, google=True)
    stats = samples.slot_stats(df)

    for origin, destination in samples.legs:
        if path := plot_leg(df, stats, origin, destination, args.days):
            print(f"{samples.leg_name(origin, destination)}: {path.relative_to(samples.repo_root)}")
        else:
            print(f"{samples.leg_name(origin, destination)}: no samples, skipped")


if __name__ == "__main__":
    main()

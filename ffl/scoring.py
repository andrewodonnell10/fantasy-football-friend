"""League scoring engine.

Every ranking, projection, and recommendation in this project is priced through
here, because this league's settings diverge from the generic assumptions baked
into public rankings:

    -1 per sack taken      unusual; devalues QBs behind bad offensive lines
    4-pt passing TD        depresses QB value relative to 6-pt leagues
    1.0 PPR                elevates high-volume slot receivers
    -2 per fumble lost     with fractional and negative scoring enabled

Two layers, deliberately separated:

    score_*()          operate on a normalised stat dict, so they can be tested
                       against hand-computed values with no data source involved
    from_nflverse()    adapts a real nflverse player_stats row into that dict

That split is what lets the scoring rules be verified independently of whether
we are reading nflverse column names correctly.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ffl.config import get_config

# ---------------------------------------------------------------------------
# Normalised stat keys -> nflverse player_stats columns.
#
# A tuple means "sum these columns". Anything absent from a row counts as zero,
# so this stays robust to nflverse adding or renaming peripheral fields.
# ---------------------------------------------------------------------------

NFLVERSE_STAT_MAP: dict[str, tuple[str, ...]] = {
    # passing
    "passing_yards": ("passing_yards",),
    "passing_tds": ("passing_tds",),
    "interceptions": ("passing_interceptions",),
    "sacks": ("sacks_suffered",),
    # rushing
    "rushing_yards": ("rushing_yards",),
    "rushing_tds": ("rushing_tds",),
    # receiving
    "receptions": ("receptions",),
    "receiving_yards": ("receiving_yards",),
    "receiving_tds": ("receiving_tds",),
    # misc offense
    "return_tds": ("special_teams_tds",),
    "offensive_fumble_return_td": ("fumble_recovery_tds",),
    "fumbles_lost": ("fumbles_lost_total",),
    # Yahoo reports a single "2-Point Conversions" line, so all three flavours
    # are summed. If `--check-settings` reports drift on this stat, split it.
    "two_point_conversions": (
        "passing_2pt_conversions",
        "rushing_2pt_conversions",
        "receiving_2pt_conversions",
    ),
    # kicking
    "pat_made": ("pat_made",),
    "fg_made_0_19": ("fg_made_0_19",),
    "fg_made_20_29": ("fg_made_20_29",),
    "fg_made_30_39": ("fg_made_30_39",),
    "fg_made_40_49": ("fg_made_40_49",),
    "fg_made_50_59": ("fg_made_50_59",),
    "fg_made_60_": ("fg_made_60_",),
}

# Which normalised key holds made field goals for each distance bucket.
FG_BUCKET_KEYS: tuple[tuple[int, str], ...] = (
    (0, "fg_made_0_19"),
    (20, "fg_made_20_29"),
    (30, "fg_made_30_39"),
    (40, "fg_made_40_49"),
    (50, "fg_made_50_59"),
    (60, "fg_made_60_"),
)


def _num(value: Any) -> float:
    """Coerce to float, treating None/NaN/blank as zero."""
    if value is None:
        return 0.0
    try:
        result = float(value)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if result != result else result  # NaN check


def _bucket_points(value: float, buckets: list[Mapping[str, Any]]) -> float:
    """Find the points for a value falling in an inclusive [min, max] bucket.

    A null `max` means the bucket is open-ended.
    """
    for bucket in buckets:
        low = bucket.get("min")
        high = bucket.get("max")
        if low is not None and value < low:
            continue
        if high is not None and value > high:
            continue
        return float(bucket["points"])
    return 0.0


class Scorer:
    """Applies this league's scoring table to normalised stat lines."""

    def __init__(self, rules: Mapping[str, Any] | None = None) -> None:
        self.rules = dict(rules or get_config().scoring_rules)
        self.offense = self.rules.get("offense", {})
        self.kicking = self.rules.get("kicking", {})
        self.defense = self.rules.get("defense", {})

    # -- component scores ---------------------------------------------------

    def score_offense(self, stats: Mapping[str, Any]) -> float:
        """Score passing/rushing/receiving production."""
        return sum(
            _num(stats.get(stat)) * float(points)
            for stat, points in self.offense.items()
        )

    def score_kicking(self, stats: Mapping[str, Any]) -> float:
        """Score PATs plus field goals, bucketed by distance."""
        total = _num(stats.get("pat_made")) * float(self.kicking.get("pat_made", 0))

        fg_buckets = self.kicking.get("field_goals", [])
        for distance, key in FG_BUCKET_KEYS:
            made = _num(stats.get(key))
            if made:
                total += made * _bucket_points(float(distance), fg_buckets)
        return total

    def score_defense(self, stats: Mapping[str, Any]) -> float:
        """Score a team defense/special-teams unit.

        `points_allowed` is scored from its own tier table; every other defensive
        stat is a flat per-event value.
        """
        total = 0.0
        for stat, points in self.defense.items():
            if stat == "points_allowed":
                continue
            total += _num(stats.get(stat)) * float(points)

        if "points_allowed" in stats:
            total += _bucket_points(
                _num(stats.get("points_allowed")),
                self.defense.get("points_allowed", []),
            )
        return total

    # -- dispatch -----------------------------------------------------------

    def score(self, stats: Mapping[str, Any], position: str | None = None) -> float:
        """Score a stat line, routing by position.

        Skill players never accrue kicking stats and vice versa, so when the
        position is unknown we sum both — the irrelevant half contributes zero.
        """
        pos = (position or "").upper()

        if pos in {"DEF", "DST", "D/ST"}:
            return round(self.score_defense(stats), 2)
        if pos == "K":
            return round(self.score_kicking(stats) + self.score_offense(stats), 2)
        if pos:
            return round(self.score_offense(stats), 2)
        return round(self.score_offense(stats) + self.score_kicking(stats), 2)

    # -- adapters -----------------------------------------------------------

    @staticmethod
    def from_nflverse(row: Mapping[str, Any]) -> dict[str, float]:
        """Normalise an nflverse player_stats row into scoring keys."""
        return {
            key: sum(_num(row.get(col)) for col in columns)
            for key, columns in NFLVERSE_STAT_MAP.items()
        }

    def score_nflverse_row(self, row: Mapping[str, Any]) -> float:
        """Convenience: normalise an nflverse row and score it."""
        return self.score(self.from_nflverse(row), row.get("position"))


_default_scorer: Scorer | None = None


def get_scorer() -> Scorer:
    """Process-wide scorer built from league_rules.yaml."""
    global _default_scorer
    if _default_scorer is None:
        _default_scorer = Scorer()
    return _default_scorer

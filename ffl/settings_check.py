"""Compare Yahoo's live league settings against docs/League_Rules_And_Settings.md.

The rules document is what the league agreed to. Yahoo's configuration is what
will actually score games. Those can drift — a commissioner mis-clicks a value,
or a setting silently resets between seasons — and the difference is only ever
noticed after it has cost someone a matchup.

Stat IDs are resolved from Yahoo's own `/game/nfl/stat_categories` catalogue at
runtime rather than hardcoded. Yahoo's numeric IDs are undocumented and easy to
transcribe wrongly; asking Yahoo what each ID means removes that guesswork
entirely, at the cost of one extra request per sync.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# Yahoo display name (normalised) -> dotted path into league_rules.yaml scoring.
# Several Yahoo skins word the same stat differently, hence the aliases.
NAME_TO_RULE: dict[str, str] = {
    # offense
    "passingyards": "offense.passing_yards",
    "passingtouchdowns": "offense.passing_tds",
    "interceptions": "offense.interceptions",
    "sacks": "offense.sacks",
    "rushingyards": "offense.rushing_yards",
    "rushingtouchdowns": "offense.rushing_tds",
    "receptions": "offense.receptions",
    "receptionyards": "offense.receiving_yards",
    "receivingyards": "offense.receiving_yards",
    "receptiontouchdowns": "offense.receiving_tds",
    "receivingtouchdowns": "offense.receiving_tds",
    "returntouchdowns": "offense.return_tds",
    "2pointconversions": "offense.two_point_conversions",
    "twopointconversions": "offense.two_point_conversions",
    "fumbleslost": "offense.fumbles_lost",
    "offensivefumblereturntd": "offense.offensive_fumble_return_td",
    # kicking
    "pointafterattemptmade": "kicking.pat_made",
    "fieldgoals019yards": "kicking.fg.0",
    "fieldgoals2029yards": "kicking.fg.20",
    "fieldgoals3039yards": "kicking.fg.30",
    "fieldgoals4049yards": "kicking.fg.40",
    "fieldgoals50yards": "kicking.fg.50",
    # defense / special teams
    "sack": "defense.sacks",
    "interception": "defense.interceptions",
    "fumblerecovery": "defense.fumble_recoveries",
    "touchdown": "defense.touchdowns",
    "safety": "defense.safeties",
    "blockkick": "defense.blocked_kicks",
    "kickoffandpuntreturntouchdowns": "defense.return_tds",
    "extrapointreturned": "defense.extra_point_returned",
    "pointsallowed0points": "defense.pa.0",
    "pointsallowed16points": "defense.pa.1",
    "pointsallowed713points": "defense.pa.7",
    "pointsallowed1420points": "defense.pa.14",
    "pointsallowed2127points": "defense.pa.21",
    "pointsallowed2834points": "defense.pa.28",
    "pointsallowed35points": "defense.pa.35",
}


@dataclass
class Difference:
    """One point of disagreement between the rules doc and Yahoo."""

    stat: str
    expected: Any
    actual: Any
    kind: str  # mismatch | missing_in_yahoo | missing_in_rules

    def __str__(self) -> str:
        if self.kind == "mismatch":
            return f"{self.stat}: rules say {self.expected}, Yahoo has {self.actual}"
        if self.kind == "missing_in_yahoo":
            return f"{self.stat}: in rules ({self.expected}) but not scored by Yahoo"
        return f"{self.stat}: Yahoo scores this ({self.actual}) but the rules doc omits it"


def normalise(name: str) -> str:
    """Fold a Yahoo stat name to a comparison key: lowercase, alphanumerics only."""
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def flatten_rules(scoring: dict) -> dict[str, float]:
    """Flatten league_rules.yaml scoring into the dotted paths NAME_TO_RULE uses."""
    flat: dict[str, float] = {}

    for stat, value in (scoring.get("offense") or {}).items():
        flat[f"offense.{stat}"] = float(value)

    kicking = scoring.get("kicking") or {}
    if "pat_made" in kicking:
        flat["kicking.pat_made"] = float(kicking["pat_made"])
    for bucket in kicking.get("field_goals") or []:
        flat[f"kicking.fg.{bucket['min']}"] = float(bucket["points"])

    defense = scoring.get("defense") or {}
    for stat, value in defense.items():
        if stat == "points_allowed":
            continue
        flat[f"defense.{stat}"] = float(value)
    for bucket in defense.get("points_allowed") or []:
        flat[f"defense.pa.{bucket['min']}"] = float(bucket["points"])

    return flat


def parse_stat_catalogue(payload: Any) -> dict[str, str]:
    """Build stat_id -> display name from Yahoo's `/game/nfl/stat_categories`."""
    catalogue: dict[str, str] = {}
    for stat in _walk_stats(payload):
        stat_id = _scalar(stat.get("stat_id"))
        name = _scalar(stat.get("display_name")) or _scalar(stat.get("name"))
        if stat_id is not None and name:
            catalogue[str(stat_id)] = str(name)
    return catalogue


def parse_stat_modifiers(payload: Any) -> dict[str, float]:
    """Build stat_id -> configured point value from a league `settings` response."""
    modifiers: dict[str, float] = {}
    for stat in _walk_stats(payload):
        stat_id = _scalar(stat.get("stat_id"))
        value = _scalar(stat.get("value"))
        if stat_id is None or value is None:
            continue
        try:
            modifiers[str(stat_id)] = float(value)
        except (TypeError, ValueError):
            continue
    return modifiers


def _scalar(value: Any) -> Any:
    """Unwrap Yahoo's `{'#text': ...}` scalar wrapping."""
    if isinstance(value, dict):
        return value.get("#text", value.get("$t"))
    return value


def _walk_stats(payload: Any) -> list[dict]:
    """Collect every dict that looks like a stat entry, at any nesting depth.

    Yahoo's XML-to-JSON conversion nests inconsistently and collapses
    single-element lists into bare dicts, so walking for shape is markedly more
    robust than indexing a fixed path.
    """
    found: list[dict] = []

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            if "stat_id" in node:
                found.append(node)
            for child in node.values():
                visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)

    visit(payload)
    return found


def compare(
    scoring_rules: dict,
    catalogue: dict[str, str],
    modifiers: dict[str, float],
) -> list[Difference]:
    """Diff the rules doc against Yahoo's configured scoring."""
    expected = flatten_rules(scoring_rules)

    actual: dict[str, float] = {}
    unrecognised: dict[str, float] = {}
    for stat_id, value in modifiers.items():
        rule_path = NAME_TO_RULE.get(normalise(catalogue.get(stat_id, "")))
        if rule_path:
            actual[rule_path] = value
        else:
            unrecognised[catalogue.get(stat_id, f"stat_id {stat_id}")] = value

    differences: list[Difference] = []

    for path, expected_value in sorted(expected.items()):
        if path not in actual:
            differences.append(Difference(path, expected_value, None, "missing_in_yahoo"))
        elif abs(actual[path] - expected_value) > 1e-6:
            differences.append(Difference(path, expected_value, actual[path], "mismatch"))

    # A stat Yahoo scores that the rules doc never mentions is also drift — it
    # silently changes outcomes — but only report non-zero values, since Yahoo
    # lists every stat in its catalogue with zeros for the ones not in use.
    for name, value in sorted(unrecognised.items()):
        if value:
            differences.append(Difference(name, None, value, "missing_in_rules"))

    return differences


def compare_rosters(expected_roster: dict, yahoo_positions: list[str]) -> list[str]:
    """Compare roster structure as a multiset, returning human-readable drift."""
    from collections import Counter

    expected = Counter()
    for position, count in (expected_roster.get("starters") or {}).items():
        expected[position] += int(count)
    expected["BN"] += int(expected_roster.get("bench", 0) or 0)
    expected["IR"] += int(expected_roster.get("ir", 0) or 0)

    actual = Counter(yahoo_positions)

    issues = []
    for position in sorted(set(expected) | set(actual)):
        if expected[position] != actual[position]:
            issues.append(
                f"{position}: rules say {expected[position]}, Yahoo has {actual[position]}"
            )
    return issues

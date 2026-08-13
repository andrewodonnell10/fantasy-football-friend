"""Keeper eligibility tests — one per branch of the league's rules."""

from __future__ import annotations

import pytest

from ffl.engines import keepers
from tests.conftest import LEAGUE_KEY


@pytest.fixture
def verdicts(season, rules) -> dict:
    return {
        e.player_key: e for e in keepers.evaluate_league(season, LEAGUE_KEY, rules["keepers"])
    }


def test_clean_seventh_rounder_is_eligible_at_round_six(verdicts):
    """The headline case from the rules doc: round 7 drafted -> round 6 keeper."""
    verdict = verdicts["p.clean"]
    assert verdict.eligible
    assert verdict.kdv_round == 6
    assert verdict.reasons == []


def test_kdv_is_always_one_round_earlier(verdicts):
    for key in ("p.hill", "p.kelce", "p.clean"):
        verdict = verdicts[key]
        if verdict.eligible:
            assert verdict.kdv_round == verdict.draft_round - 1


def test_first_round_picks_are_never_eligible(verdicts):
    for key in ("p.jefferson", "p.mccaffrey"):
        verdict = verdicts[key]
        assert not verdict.eligible
        assert verdict.kdv_round is None
        assert any("round 1" in r for r in verdict.reasons)


def test_dropped_player_is_ineligible_with_a_date(verdicts):
    verdict = verdicts["p.dropped"]
    assert not verdict.eligible
    assert "Dropped on 2025-09-11" in verdict.reasons


def test_traded_player_is_ineligible(verdicts):
    verdict = verdicts["p.traded"]
    assert not verdict.eligible
    assert any("Traded" in r for r in verdict.reasons)


def test_drop_then_readd_fails_on_both_counts(verdicts):
    """Re-acquiring your own dropped player does not restore eligibility."""
    verdict = verdicts["p.rejoined"]
    assert not verdict.eligible
    assert any("Dropped" in r for r in verdict.reasons)
    assert any("waivers" in r for r in verdict.reasons)


def test_player_missing_from_final_roster_is_flagged(verdicts):
    """A player who left the roster with no transaction explaining it.

    This is the consistency check — it should surface as a reason rather than
    silently passing, because it means our transaction data is incomplete.
    """
    verdict = verdicts["p.vanished"]
    assert not verdict.eligible
    assert any("roster" in r.lower() for r in verdict.reasons)


def test_undrafted_players_are_never_offered(verdicts):
    """You must have originally drafted a player to keep them."""
    assert "p.undrafted" not in verdicts


def test_every_pick_gets_a_verdict(verdicts):
    """No drafted player silently disappears — 9 picks in, 9 verdicts out."""
    assert len(verdicts) == 9


def test_ineligible_players_always_explain_themselves(verdicts):
    for verdict in verdicts.values():
        if not verdict.eligible:
            assert verdict.reasons, f"{verdict.player_name} is ineligible with no reason given"


def test_reasons_are_deduplicated(season, rules):
    """A player dropped twice should say so once."""
    season.execute(
        "INSERT INTO transactions (transaction_key, league_key, type, status, timestamp) "
        "VALUES ('x.9', ?, 'drop', 'successful', 1726000000)",
        (LEAGUE_KEY,),
    )
    season.execute(
        "INSERT INTO transaction_players "
        "(transaction_key, player_key, type, source_type, destination_type) "
        "VALUES ('x.9', 'p.dropped', 'drop', 'team', 'waivers')"
    )
    season.commit()

    verdict = {
        e.player_key: e for e in keepers.evaluate_league(season, LEAGUE_KEY, rules["keepers"])
    }["p.dropped"]
    assert len(verdict.reasons) == len(set(verdict.reasons))


def test_grouping_by_team_puts_eligible_first(season, rules):
    grouped = keepers.evaluate_by_team(season, LEAGUE_KEY, rules["keepers"])
    assert set(grouped) == {"t.1", "t.2"}
    for entries in grouped.values():
        eligibility = [e.eligible for e in entries]
        assert eligibility == sorted(eligibility, reverse=True)


def test_declaration_deadline_is_a_week_before_the_draft(season, rules):
    """Draft time 1756400000 is 2025-08-28; the deadline is seven days earlier."""
    deadline = keepers.declaration_deadline(season, LEAGUE_KEY, rules["keepers"])
    assert deadline is not None
    assert deadline.strftime("%Y-%m-%d") == "2025-08-21"


def test_selections_round_trip(season, rules):
    keepers.save_selections(
        season,
        LEAGUE_KEY,
        2026,
        "t.2",
        [{"player_key": "p.clean", "kdv_round": 6, "note": "obvious keep"}],
        rules["keepers"],
    )
    rows = season.execute(
        "SELECT player_key, kdv_round, kdv_source, note FROM keeper_selections "
        "WHERE team_key='t.2' AND season=2026"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["player_key"] == "p.clean"
    assert rows[0]["kdv_round"] == 6
    assert rows[0]["kdv_source"] == "round_minus_one"


def test_selections_are_replaced_not_appended(season, rules):
    """Re-saving a team's picks replaces them, so the max-2 rule can't be evaded."""
    for player in ("p.clean", "p.kelce"):
        keepers.save_selections(
            season, LEAGUE_KEY, 2026, "t.2",
            [{"player_key": player, "kdv_round": 6}], rules["keepers"],
        )
    rows = season.execute(
        "SELECT player_key FROM keeper_selections WHERE team_key='t.2' AND season=2026"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["player_key"] == "p.kelce"


def test_cannot_save_more_than_two_keepers(season, rules):
    with pytest.raises(ValueError, match="at most 2"):
        keepers.save_selections(
            season, LEAGUE_KEY, 2026, "t.1",
            [
                {"player_key": "p.hill", "kdv_round": 1},
                {"player_key": "p.rejoined", "kdv_round": 5},
                {"player_key": "p.vanished", "kdv_round": 7},
            ],
            rules["keepers"],
        )


def test_round_two_keeper_is_flagged_as_costing_a_first(verdicts):
    """A round-2 pick has a KDV of round 1, which consumes a first-round pick.

    The rules exclude round-1 draftees but say nothing about round-1 keeper
    values, so this stays eligible — but it must be surfaced, not buried.
    """
    verdict = verdicts["p.hill"]
    assert verdict.eligible
    assert verdict.kdv_round == 1
    assert verdict.costs_first_rounder
    assert verdict.cautions


def test_normal_keeper_carries_no_caution(verdicts):
    verdict = verdicts["p.clean"]
    assert verdict.eligible
    assert not verdict.costs_first_rounder
    assert verdict.cautions == []

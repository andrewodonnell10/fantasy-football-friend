"""Scoring engine tests against hand-computed values.

Every expected number below is worked out longhand in its docstring, so a
failure points at either the rules file or the engine, never at "the number
looked about right".
"""

from __future__ import annotations

import pytest

from ffl.scoring import Scorer


@pytest.fixture
def scorer(rules) -> Scorer:
    return Scorer(rules["scoring"])


def test_quarterback_line(scorer: Scorer):
    """300 pass yds, 2 pass TD, 1 INT, 3 sacks, 20 rush yds, 1 rush TD.

    300 * 0.04 = 12.0      (25 yards per point)
      2 * 4     =  8.0
      1 * -1    = -1.0
      3 * -1    = -3.0     (sacks taken — this league's unusual rule)
     20 * 0.1   =  2.0
      1 * 6     =  6.0
                 ------
                  24.0
    """
    stats = {
        "passing_yards": 300,
        "passing_tds": 2,
        "interceptions": 1,
        "sacks": 3,
        "rushing_yards": 20,
        "rushing_tds": 1,
    }
    assert scorer.score(stats, "QB") == 24.0


def test_sack_penalty_is_applied(scorer: Scorer):
    """The -1 per sack is the league's most unusual rule; verify it in isolation."""
    base = {"passing_yards": 250, "passing_tds": 1}
    sacked = {**base, "sacks": 5}
    assert scorer.score(base, "QB") - scorer.score(sacked, "QB") == 5.0


def test_full_ppr_receiver(scorer: Scorer):
    """8 receptions, 95 receiving yds, 1 TD.

      8 * 1    =  8.0     (full PPR)
     95 * 0.1  =  9.5     (10 yards per point)
      1 * 6    =  6.0
                ------
                 23.5
    """
    stats = {"receptions": 8, "receiving_yards": 95, "receiving_tds": 1}
    assert scorer.score(stats, "WR") == 23.5


def test_fractional_scoring_survives(scorer: Scorer):
    """37 receiving yards must score 3.7, not round to 4 — fractional is on."""
    assert scorer.score({"receiving_yards": 37}, "WR") == 3.7


def test_negative_total_is_allowed(scorer: Scorer):
    """2 INTs and 2 fumbles lost with no production is negative, not floored at 0."""
    stats = {"interceptions": 2, "fumbles_lost": 2}
    assert scorer.score(stats, "QB") == -6.0


def test_kicker_distance_buckets(scorer: Scorer):
    """1 FG in 0-19, 1 in 40-49, 1 in 50-59, plus 3 PATs.

      0-19  -> 2
     40-49  -> 3
     50-59  -> 4
     3 PAT  -> 3
             ---
              12
    """
    stats = {
        "fg_made_0_19": 1,
        "fg_made_40_49": 1,
        "fg_made_50_59": 1,
        "pat_made": 3,
    }
    assert scorer.score(stats, "K") == 12.0


def test_long_field_goals_score_four(scorer: Scorer):
    """Both 50-59 and 60+ fall in the league's open-ended 50+ bucket at 4 points."""
    assert scorer.score({"fg_made_50_59": 1}, "K") == 4.0
    assert scorer.score({"fg_made_60_": 1}, "K") == 4.0


def test_defense_with_points_allowed_tier(scorer: Scorer):
    """3 sacks, 2 INT, 1 fumble recovery, 1 TD, 10 points allowed.

      3 * 1  = 3
      2 * 2  = 4
      1 * 2  = 2
      1 * 6  = 6
     PA 10   = 4     (falls in the 7-13 tier)
              ---
               19
    """
    stats = {
        "sacks": 3,
        "interceptions": 2,
        "fumble_recoveries": 1,
        "touchdowns": 1,
        "points_allowed": 10,
    }
    assert scorer.score(stats, "DEF") == 19.0


@pytest.mark.parametrize(
    "points_allowed,expected",
    [(0, 10), (3, 7), (6, 7), (7, 4), (13, 4), (14, 1), (20, 1), (21, 0), (27, 0), (28, -1), (34, -1), (35, -4), (52, -4)],
)
def test_points_allowed_tiers_including_boundaries(scorer: Scorer, points_allowed, expected):
    """Every tier boundary, since off-by-one here silently mis-scores every week."""
    assert scorer.score({"points_allowed": points_allowed}, "DEF") == float(expected)


def test_shutout_scores_ten(scorer: Scorer):
    assert scorer.score({"points_allowed": 0}, "DEF") == 10.0


def test_blowout_loss_is_negative(scorer: Scorer):
    assert scorer.score({"points_allowed": 41}, "DEF") == -4.0


def test_missing_stats_count_as_zero(scorer: Scorer):
    """Sparse and None-valued stat lines must not raise."""
    assert scorer.score({}, "WR") == 0.0
    assert scorer.score({"receiving_yards": None, "receptions": 4}, "WR") == 4.0


def test_nflverse_adapter_maps_real_column_names(scorer: Scorer):
    """The adapter must read nflverse's actual column names.

    `sacks_suffered` and `passing_interceptions` are the two most likely to be
    mis-mapped, since the obvious guesses (`sacks`, `interceptions`) are wrong.
    """
    row = {
        "position": "QB",
        "passing_yards": 275,
        "passing_tds": 3,
        "passing_interceptions": 1,
        "sacks_suffered": 2,
        "rushing_yards": 15,
        "fumbles_lost_total": 1,
    }
    # 275*0.04=11.0, +3*4=12.0, -1, -2, +1.5, -2  ->  19.5
    assert scorer.score_nflverse_row(row) == 19.5


def test_two_point_conversions_sum_across_types(scorer: Scorer):
    """Yahoo reports one 2-pt line, so passing/rushing/receiving all count."""
    row = {
        "position": "RB",
        "rushing_2pt_conversions": 1,
        "receiving_2pt_conversions": 1,
    }
    assert scorer.score_nflverse_row(row) == 4.0

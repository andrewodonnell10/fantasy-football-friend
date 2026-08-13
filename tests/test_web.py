"""Web UI route tests against the synthetic season."""

from __future__ import annotations

import sqlite3

from tests.conftest import LEAGUE_KEY


def test_dashboard_renders(client):
    response = client.get("/")
    assert response.status_code == 200
    assert b"Dashboard" in response.data


def test_dashboard_shows_declaration_deadline(client):
    """Draft time is 2025-08-28, so the deadline shown is 2025-08-21."""
    body = client.get("/").get_data(as_text=True)
    assert "Aug 21, 2025" in body


def test_keeper_board_lists_both_teams(client):
    body = client.get("/keepers").get_data(as_text=True)
    assert "Team One" in body
    assert "Team Two" in body


def test_keeper_board_shows_kdv_for_eligible_player(client):
    """The clean round-7 pick must be offered at round 6."""
    body = client.get("/keepers").get_data(as_text=True)
    assert "Clean Keeper" in body
    assert "Round 6" in body


def test_keeper_board_explains_ineligibility(client):
    """Ineligible players appear with a reason, not silently dropped."""
    body = client.get("/keepers").get_data(as_text=True)
    assert "Justin Jefferson" in body          # round 1 pick
    assert "round 1 picks cannot be kept" in body
    assert "Dropped on 2025-09-11" in body


def test_team_page_renders(client):
    response = client.get("/keepers/t.2")
    assert response.status_code == 200
    assert b"Team Two" in response.data


def test_unknown_team_is_404(client):
    assert client.get("/keepers/t.99").status_code == 404


def test_saving_keepers_persists(client, seeded_db_path):
    response = client.post(
        "/keepers/t.2",
        data={"player_key": "p.clean", "note": "obvious"},
        follow_redirects=True,
    )
    assert response.status_code == 200

    conn = sqlite3.connect(seeded_db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT player_key, kdv_round, note FROM keeper_selections WHERE team_key='t.2'"
    ).fetchall()
    conn.close()

    assert len(rows) == 1
    assert rows[0]["player_key"] == "p.clean"
    assert rows[0]["kdv_round"] == 6
    assert rows[0]["note"] == "obvious"


def test_cannot_save_more_than_two(client, seeded_db_path):
    """Team One has fewer than 3 eligible, so send Team Two's whole pool plus extras."""
    response = client.post(
        "/keepers/t.2",
        data={"player_key": ["p.clean", "p.kelce", "p.mccaffrey"]},
        follow_redirects=True,
    )
    assert b"at most 2" in response.data

    conn = sqlite3.connect(seeded_db_path)
    count = conn.execute("SELECT COUNT(*) FROM keeper_selections").fetchone()[0]
    conn.close()
    assert count == 0


def test_cannot_save_an_ineligible_player(client, seeded_db_path):
    """A hand-crafted POST must not be able to declare a round-1 pick."""
    response = client.post(
        "/keepers/t.2",
        data={"player_key": "p.mccaffrey"},
        follow_redirects=True,
    )
    assert b"not keeper-eligible" in response.data

    conn = sqlite3.connect(seeded_db_path)
    count = conn.execute("SELECT COUNT(*) FROM keeper_selections").fetchone()[0]
    conn.close()
    assert count == 0


def test_draft_page_renders(client):
    body = client.get("/draft").get_data(as_text=True)
    assert "Clean Keeper" in body
    assert "2025 draft" in body


def test_transactions_page_and_filter(client):
    assert b"Dropped Guy" in client.get("/transactions").data

    filtered = client.get("/transactions?type=trade").get_data(as_text=True)
    assert "Traded Guy" in filtered
    assert "Dropped Guy" not in filtered


def test_player_page_shows_verdict_and_timeline(client):
    body = client.get("/players/p.rejoined").get_data(as_text=True)
    assert "Not eligible" in body
    assert "Dropped on 2025-10-14" in body


def test_player_page_for_clean_keeper(client):
    body = client.get("/players/p.clean").get_data(as_text=True)
    assert "Eligible" in body
    assert "round 6" in body
    assert "Never involved in a transaction" in body


def test_undrafted_player_page_explains_itself(client):
    body = client.get("/players/p.undrafted").get_data(as_text=True)
    assert "must have originally drafted" in body


def test_unknown_player_is_404(client):
    assert client.get("/players/p.nobody").status_code == 404


def test_standings_page_renders(client):
    assert client.get("/standings").status_code == 200


def test_empty_database_gives_guidance(tmp_path):
    """With no data, the UI must say what command to run rather than error."""
    from ffl.web.app import create_app

    app = create_app(str(tmp_path / "blank.db"))
    app.config.update(TESTING=True)
    body = app.test_client().get("/keepers").get_data(as_text=True)
    assert "fetch_league.py" in body


def test_board_flags_a_keeper_that_costs_a_first_rounder(client):
    body = client.get("/keepers").get_data(as_text=True)
    assert "costs your 1st" in body


def test_team_page_explains_the_first_round_caution(client):
    body = client.get("/keepers/t.1").get_data(as_text=True)
    assert "costs your first-round pick" in body

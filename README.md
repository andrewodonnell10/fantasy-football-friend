# Fantasy Football Friend

A personal decision engine for a Yahoo keeper league. It pulls league data into
SQLite, prices every player through *this* league's scoring rules rather than
generic rankings, and serves a local web UI for making decisions.

The first question it answers is **who to keep**, and that has a deadline:
keepers must be declared to the commissioner one week before the draft.

## What it does today

**Phase 1 — keepers (built)**

- Pulls the 2025 draft, transaction log, rosters, and standings from Yahoo
- Computes keeper eligibility for every team, showing *why* each ineligible
  player failed rather than silently omitting them
- Records your selections — which matters more than it looks, see below
- Counts down to the declaration deadline
- Flags drift between Yahoo's live settings and the league rules document

**Phases 2 and 3 — draft board and in-season engines (planned)**

Keeper-adjusted draft board, then start/sit, waivers, and trade analysis. The
data backbone those need is already built and syncing.

## Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env          # then fill in your Yahoo credentials
```

Creating the Yahoo app is the one manual prerequisite — see
**[SETUP_YAHOO_APP.md](SETUP_YAHOO_APP.md)**. The redirect URI must be
`https://localhost:8000`, HTTPS not HTTP, which is the single most common
setup mistake.

```bash
yahoofantasy login            # one-time browser OAuth
python scripts/preflight.py   # verify everything is wired
```

## Usage

```bash
# Find your league ids, put them in .env
python scripts/fetch_league.py --discover --season 2025

# Pull the season that determines keeper eligibility
python scripts/fetch_league.py --sync --season 2025

# Pull 2026 for the draft date and current settings
python scripts/fetch_league.py --sync --season 2026

# Pull the nflverse analytics backbone (no Yahoo credentials needed)
python scripts/fetch_league.py --sync-nflverse --season 2025

# Check Yahoo's configured scoring against the rules document
python scripts/fetch_league.py --check-settings --season 2026

# Keeper board in the terminal
python scripts/fetch_league.py --keepers --season 2025

# Or the web UI
python -m ffl.web.app         # http://127.0.0.1:5001
```

Syncing is idempotent — re-run it as often as you like, and re-run it after any
interruption. Nothing duplicates.

## The keeper rules, as implemented

From [docs/League_Rules_And_Settings.md](docs/League_Rules_And_Settings.md).
A player is eligible for the team that drafted them if **all** of these hold:

| Condition | Where it comes from |
|---|---|
| They drafted the player in 2025 | draft results |
| Drafted in round 2 or later | round 1 picks are excluded |
| Never dropped, at any point | transaction log |
| Never picked up from waivers or FA | transaction log |
| Never traded | transaction log |
| Still on that team's final roster | consistency check |

**Keeper Draft Value = draft round − 1.** Drafted in round 7, kept as a round 6
pick. Each team may keep up to two.

### Two things worth knowing

**A round-2 pick costs your first-rounder.** Its KDV is round 1. The rules
exclude players *drafted* in round 1 but say nothing about keepers *valued* at
round 1, so the tool allows it and flags it. Worth confirming with your
commissioner before relying on it.

**Recording your selections is not optional bookkeeping.** Yahoo never marks who
was a keeper — a kept player appears as an ordinary draft pick in their KDV
round. The rule allowing a second consecutive keep (priced at prior-season ADP)
is dormant in 2026 because nobody has been kept yet, but it becomes
uncomputable in 2027 unless this year's keepers are written down. That is what
the `keeper_selections` table is for.

## Data sources

| Source | Auth | What it provides |
|---|---|---|
| [Yahoo Fantasy API](https://developer.yahoo.com/fantasysports/guide/) | OAuth2 (read-only) | League truth: rosters, transactions, draft, settings |
| [nflverse](https://github.com/nflverse/nflreadpy) | none | Weekly stats, snap counts, injuries, schedules — CC-BY 4.0 |
| [ffverse player IDs](https://github.com/dynastyprocess/data) | none | Yahoo ↔ nflverse ↔ Sleeper ↔ ESPN ID crosswalk |
| [Sleeper](https://docs.sleeper.com/) | none | Trending adds/drops (Phase 3) |
| ESPN (undocumented) | none | Projections and game odds (Phase 3, off by default) |
| [Open-Meteo](https://open-meteo.com/) | none | Stadium weather (Phase 3) |

Yahoo access is **read-only** by design. This project will not make roster moves.

## Scoring

`ffl/scoring.py` prices everything through `ffl/league_rules.yaml`, encoded from
the rules document. Public rankings assume defaults this league does not use:

- **−1 per sack taken** — devalues QBs behind poor offensive lines
- **4-point passing TDs** at 25 yards per point
- **Full PPR** at 10 yards per point receiving
- Fractional and negative scoring, −2 per fumble lost
- Tiered DEF points-allowed (10 down to −4), distance-tiered field goals

The effect is visible in real data: over the 2025 regular season, top QBs score
around 20 points per game under these rules while top RBs clear 24.

## Layout

```
ffl/
  config.py         .env loading and validation
  league_rules.yaml keeper constraints + full scoring table
  scoring.py        applies the league's scoring to any stat line
  schema.sql db.py  SQLite storage, idempotent upserts
  settings_check.py Yahoo settings vs the rules document
  sources/          yahoo.py, nflverse.py
  engines/          keepers.py
  web/              Flask UI, 127.0.0.1 only
scripts/
  preflight.py      connectivity + credential diagnostic
  fetch_league.py   discovery, syncing, keeper board
tests/              73 tests, no network required
```

## Tests

```bash
pytest
```

Runs against a synthetic season covering every branch of the keeper rules, plus
hand-computed scoring cases and UI route checks. No network access needed.

The Yahoo fixtures are hand-written from documented response shapes — they
verify our logic, not our reading of Yahoo's format. Run
`--sync --save-fixtures` after your first successful login to capture real
responses, then re-check the parsing.

## Notes

- The web UI binds to `127.0.0.1` only and has no authentication. It holds
  private league data; do not expose it.
- `.env`, `*.db`, and the token cache are gitignored. Credentials are read from
  the environment and never appear in source.
- Yahoo publishes no documented rate limit; syncs space their requests and
  cache responses rather than polling.

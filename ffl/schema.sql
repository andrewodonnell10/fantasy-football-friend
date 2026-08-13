-- Fantasy Football Friend — SQLite schema
--
-- Design notes:
--   * Primary keys are Yahoo's own natural keys, so re-running a sync upserts
--     rather than duplicating. Sync is safe to run as often as you like.
--   * Core tables carry a `raw_json` column holding the untouched API payload.
--     That means new fields can be back-derived later without re-hitting a
--     rate-limited API, and a parsing bug is recoverable rather than lossy.
--   * Yahoo league keys look like `nfl.l.123456` — the separator is a lowercase
--     L, not the digit 1.

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------------
-- League structure
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS leagues (
    league_key      TEXT PRIMARY KEY,
    league_id       TEXT NOT NULL,
    game_key        TEXT NOT NULL,
    season          INTEGER NOT NULL,
    name            TEXT,
    num_teams       INTEGER,
    scoring_type    TEXT,
    draft_type      TEXT,
    draft_status    TEXT,
    draft_time      TEXT,               -- ISO 8601; drives the keeper deadline
    is_keeper       INTEGER,
    current_week    INTEGER,
    raw_json        TEXT,
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_leagues_season ON leagues(season);

CREATE TABLE IF NOT EXISTS teams (
    team_key        TEXT PRIMARY KEY,
    league_key      TEXT NOT NULL REFERENCES leagues(league_key) ON DELETE CASCADE,
    team_id         INTEGER NOT NULL,
    name            TEXT,
    manager_name    TEXT,
    logo_url        TEXT,
    raw_json        TEXT,
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_teams_league ON teams(league_key);

CREATE TABLE IF NOT EXISTS standings (
    league_key      TEXT NOT NULL REFERENCES leagues(league_key) ON DELETE CASCADE,
    team_key        TEXT NOT NULL REFERENCES teams(team_key) ON DELETE CASCADE,
    captured_at     TEXT NOT NULL,
    rank            INTEGER,
    wins            INTEGER,
    losses          INTEGER,
    ties            INTEGER,
    points_for      REAL,
    points_against  REAL,
    raw_json        TEXT,
    PRIMARY KEY (league_key, team_key, captured_at)
);

-- ---------------------------------------------------------------------------
-- Players and identity
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS players (
    player_key      TEXT PRIMARY KEY,   -- e.g. 461.p.30121
    player_id       TEXT NOT NULL,      -- e.g. 30121 (stable across seasons)
    name            TEXT,
    position        TEXT,
    nfl_team        TEXT,
    bye_week        INTEGER,
    raw_json        TEXT,
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_players_player_id ON players(player_id);

-- Cross-source identity crosswalk, sourced from nflverse load_ff_playerids().
-- This is the keystone: without a reliable Yahoo -> gsis join, no other data
-- source can be attached to the user's actual roster.
CREATE TABLE IF NOT EXISTS player_ids (
    gsis_id         TEXT PRIMARY KEY,
    name            TEXT,
    position        TEXT,
    team            TEXT,
    yahoo_id        TEXT,
    sleeper_id      TEXT,
    espn_id         TEXT,
    pfr_id          TEXT,
    fantasypros_id  TEXT,
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_player_ids_yahoo ON player_ids(yahoo_id);
CREATE INDEX IF NOT EXISTS idx_player_ids_sleeper ON player_ids(sleeper_id);

CREATE TABLE IF NOT EXISTS rosters (
    league_key         TEXT NOT NULL REFERENCES leagues(league_key) ON DELETE CASCADE,
    team_key           TEXT NOT NULL REFERENCES teams(team_key) ON DELETE CASCADE,
    week               INTEGER NOT NULL,
    player_key         TEXT NOT NULL,
    selected_position  TEXT,
    is_starting        INTEGER,
    raw_json           TEXT,
    PRIMARY KEY (league_key, team_key, week, player_key)
);

CREATE INDEX IF NOT EXISTS idx_rosters_player ON rosters(player_key);

-- ---------------------------------------------------------------------------
-- Draft — `round` is what drives Keeper Draft Value
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS draft_picks (
    league_key      TEXT NOT NULL REFERENCES leagues(league_key) ON DELETE CASCADE,
    pick            INTEGER NOT NULL,
    round           INTEGER NOT NULL,
    team_key        TEXT NOT NULL,
    player_key      TEXT NOT NULL,
    cost            REAL,               -- auction drafts only
    raw_json        TEXT,
    PRIMARY KEY (league_key, pick)
);

CREATE INDEX IF NOT EXISTS idx_draft_player ON draft_picks(league_key, player_key);
CREATE INDEX IF NOT EXISTS idx_draft_team ON draft_picks(league_key, team_key);

-- ---------------------------------------------------------------------------
-- Transactions — the source of every keeper-voiding event
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS transactions (
    transaction_key TEXT PRIMARY KEY,
    league_key      TEXT NOT NULL REFERENCES leagues(league_key) ON DELETE CASCADE,
    type            TEXT,               -- add | drop | add/drop | trade | commish
    status          TEXT,
    timestamp       INTEGER,            -- unix seconds
    raw_json        TEXT
);

CREATE INDEX IF NOT EXISTS idx_transactions_league ON transactions(league_key);

-- One row per player movement inside a transaction.
--   type              : 'add' or 'drop'
--   source_type       : 'waivers' | 'freeagents' | 'team'
--   destination_type  : 'team' | 'waivers' | 'freeagents'
-- A waiver/FA pickup is type='add' with source_type in (waivers, freeagents).
CREATE TABLE IF NOT EXISTS transaction_players (
    transaction_key     TEXT NOT NULL REFERENCES transactions(transaction_key) ON DELETE CASCADE,
    player_key          TEXT NOT NULL,
    type                TEXT NOT NULL,
    source_type         TEXT,
    source_team_key     TEXT,
    destination_type    TEXT,
    destination_team_key TEXT,
    PRIMARY KEY (transaction_key, player_key, type)
);

CREATE INDEX IF NOT EXISTS idx_txn_players_player ON transaction_players(player_key);

-- ---------------------------------------------------------------------------
-- Keeper selections
--
-- Yahoo never records who was a keeper — a kept player appears as an ordinary
-- draft pick in their KDV round. This table is our own record, and it is the
-- only thing that will make the "kept twice consecutively" rule computable in
-- 2027. kdv_source is 'round_minus_one' now; 'prior_adp' becomes possible once
-- a player is being kept for a second consecutive year.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS keeper_selections (
    league_key      TEXT NOT NULL,
    season          INTEGER NOT NULL,
    team_key        TEXT NOT NULL,
    player_key      TEXT NOT NULL,
    kdv_round       INTEGER,
    kdv_source      TEXT NOT NULL DEFAULT 'round_minus_one',
    adp_round       REAL,               -- populated only when kdv_source='prior_adp'
    keep_number     INTEGER NOT NULL DEFAULT 1,   -- 1st or 2nd consecutive keep
    declared_at     TEXT,
    note            TEXT,
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (league_key, season, team_key, player_key)
);

CREATE INDEX IF NOT EXISTS idx_keeper_season ON keeper_selections(season, team_key);

-- ---------------------------------------------------------------------------
-- nflverse analytics tables (Phase 2/3 inputs)
-- ---------------------------------------------------------------------------

-- season_type separates REG from POST. Fantasy valuations must use REG only —
-- playoff games are not part of any fantasy season and would inflate totals.
CREATE TABLE IF NOT EXISTS weekly_stats (
    season          INTEGER NOT NULL,
    week            INTEGER NOT NULL,
    gsis_id         TEXT NOT NULL,
    season_type     TEXT NOT NULL DEFAULT 'REG',
    player_name     TEXT,
    position        TEXT,
    team            TEXT,
    opponent        TEXT,
    stats_json      TEXT NOT NULL,      -- full stat line, scored by ffl.scoring
    league_points   REAL,               -- computed via this league's rules
    PRIMARY KEY (season, week, gsis_id, season_type)
);

CREATE INDEX IF NOT EXISTS idx_weekly_reg ON weekly_stats(season, season_type, gsis_id);

CREATE INDEX IF NOT EXISTS idx_weekly_gsis ON weekly_stats(gsis_id);

CREATE TABLE IF NOT EXISTS snap_counts (
    season          INTEGER NOT NULL,
    week            INTEGER NOT NULL,
    pfr_id          TEXT NOT NULL,
    player_name     TEXT,
    position        TEXT,
    team            TEXT,
    offense_snaps   INTEGER,
    offense_pct     REAL,
    PRIMARY KEY (season, week, pfr_id)
);

CREATE TABLE IF NOT EXISTS injuries (
    season          INTEGER NOT NULL,
    week            INTEGER NOT NULL,
    gsis_id         TEXT NOT NULL,
    player_name     TEXT,
    team            TEXT,
    report_status   TEXT,
    practice_status TEXT,
    PRIMARY KEY (season, week, gsis_id)
);

CREATE TABLE IF NOT EXISTS schedules (
    game_id         TEXT PRIMARY KEY,
    season          INTEGER NOT NULL,
    week            INTEGER,
    gameday         TEXT,
    home_team       TEXT,
    away_team       TEXT,
    roof            TEXT,               -- outdoor games are the ones weather affects
    stadium         TEXT,
    spread_line     REAL,
    total_line      REAL,
    raw_json        TEXT
);

CREATE INDEX IF NOT EXISTS idx_schedules_season_week ON schedules(season, week);

-- ---------------------------------------------------------------------------
-- Sync audit log
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS sync_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source          TEXT NOT NULL,      -- yahoo | nflverse | sleeper | espn | weather
    resource        TEXT NOT NULL,
    season          INTEGER,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    status          TEXT NOT NULL,      -- running | ok | error
    rows_written    INTEGER,
    error           TEXT
);

CREATE INDEX IF NOT EXISTS idx_sync_recent ON sync_runs(source, resource, started_at DESC);

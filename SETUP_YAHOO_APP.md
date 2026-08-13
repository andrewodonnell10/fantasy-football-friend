# Creating the Yahoo Developer app

Nothing in this project can read your league until a Yahoo app exists. It takes
about five minutes and is free.

## 1. Create the app

Go to **https://developer.yahoo.com/apps/create/** (sign in with the Yahoo
account that is *in the league* — the app can only see leagues that account
belongs to).

Fill in:

| Field | Value |
|---|---|
| Application Name | anything, e.g. `Fantasy Football Friend` |
| Application Type | **Web Application** |
| Redirect URI | **`https://localhost:8000`** |
| API Permissions | tick **Fantasy Sports**, and choose **Read** |

### The redirect URI is the part people get wrong

It must be **`https://localhost:8000`** — HTTPS, not HTTP.

The `yahoofantasy` package spins up a local HTTPS listener using a self-signed
certificate it ships with, and defaults to that exact URI. If what you register
here differs by even a character — `http://`, a trailing slash, a different port
— the OAuth callback fails with a redirect-mismatch error.

If you would rather use plain HTTP, register `http://localhost:8000` and add
`--redirect-http` to the login command in step 3.

Read permission is all this project needs. It never writes to Yahoo: no roster
moves, no waiver claims, no trades. It tells you what to do; you do it in Yahoo.

## 2. Put the credentials in `.env`

After creating the app, Yahoo shows a **Client ID** (long, starts `dj0y…`) and a
**Client Secret**.

```bash
cp .env.example .env
```

Then edit `.env`:

```
YAHOO_CLIENT_ID=dj0yJmk9...
YAHOO_CLIENT_SECRET=abc123...
YAHOO_REDIRECT_URI=https://localhost:8000
```

`.env` is gitignored. Never commit it, and never paste these values into a
script — everything reads them from the environment.

## 3. Authenticate

```bash
source venv/bin/activate
yahoofantasy login
```

This opens a browser asking you to authorise the app. **You will see a
certificate warning** — that is the self-signed localhost certificate, and it is
expected. Click through it ("Advanced" → "Proceed"). Without doing so the token
is never saved.

A refresh token is then stored locally, so this is a one-time step.

> This must run on a machine with a browser and a usable `localhost`. It cannot
> be completed in a remote or headless environment.

## 4. Verify

```bash
python scripts/preflight.py
```

Every row should pass. If one doesn't, the message tells you which piece is
missing.

## 5. Find your league IDs

```bash
python scripts/fetch_league.py --discover --season 2025
python scripts/fetch_league.py --discover --season 2026
```

Copy the numeric `league_id` from each into `.env` as `LEAGUE_ID_2025` and
`LEAGUE_ID_2026`.

Both seasons are needed. 2025 holds the draft and transactions that determine
keeper eligibility; 2026 holds the draft date that sets the declaration
deadline. They are different leagues to Yahoo, with different keys.

## Troubleshooting

**"redirect_uri_mismatch"** — what's registered on the app doesn't match what
`yahoofantasy login` sent. Check the scheme (`https` vs `http`), the port, and
any trailing slash.

**Browser warns the certificate is invalid** — expected, see step 3. Proceed
past it.

**"Invalid client"** — the Client ID or Secret in `.env` has a typo, or a stray
space. `python scripts/preflight.py` prints them masked so you can confirm
they're being read at all.

**Login succeeds but `--discover` finds no leagues** — you authenticated with a
Yahoo account that isn't in the league. Log out of Yahoo, then run
`yahoofantasy login` again with the right account.

**Token stops working later** — delete the cached token and re-run
`yahoofantasy login`:

```bash
rm -rf ~/.yahoofantasy
yahoofantasy login
```

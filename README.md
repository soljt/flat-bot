# flatbot

**A notify-first apartment-hunting bot for Zurich.** Polls five Swiss rental platforms on a configurable interval, deduplicates matches across platforms, and sends LLM-generated email alerts to a mailing list. You then apply by hand, so the bot never logs in or accidentally sends out your sensitive info.

---

## Why this exists

Finding a flat in Zurich is competitive. Listings disappear within hours. The major portals i.e. Flatfox, Homegate, ImmoScout24, NewHome, and Comparis each have their own API shapes and bot-detection layers. Checking five sites manually every 15 minutes is a pain.

Flatbot runs continuously in Docker (or on a Raspberry Pi), normalises listings across platforms, and fires an email the moment something new appears. The hard part is bypassing Cloudflare Managed Challenges and DataDome on multiple sites, deduplicating the same flat when it's listed on two portals, and keeping the system crash-safe.

---

## Features

- **Five platform adapters** — Flatfox, Homegate, ImmoScout24, NewHome.ch, Comparis
- **Bot-detection bypass** — three transport strategies (plain httpx, FlareSolverr, nodriver headful Chrome) matched to each site's protection layer
- **Cross-platform deduplication** — content-addressed match store (`matches.jsonl`) prevents double-notifying the same flat seen on multiple portals; the second URL is appended to the existing Google Sheets row instead
- **LLM-generated emails** — Anthropic API writes the subject line and body from listing data; plain-text template fallback if the API is unavailable
- **Google Sheets log** — optional append-only match log with a blank "Human Sent Message" column for tracking outreach
- **Crash-safe stores** — `seen.txt` and `matches.jsonl` are append-only and written only after a confirmed send; a crash between send and write causes one harmless duplicate rather than a missed notification
- **Docker + Raspberry Pi support** — multi-arch build (`linux/arm64`); Xvfb virtual display lets nodriver Chrome run headfully with no physical screen
- **Dry-run and seed modes** — iterate the full pipeline without sending anything; seed current inventory before going live to avoid a first-run notification flood

---

## Tech stack

| Layer | Technology |
|---|---|
| Language | Python 3.11+ |
| Package manager | [uv](https://docs.astral.sh/uv/) |
| HTTP (plain) | [httpx](https://www.python-httpx.org/) |
| HTTP (CF bypass) | [FlareSolverr](https://github.com/FlareSolverr/FlareSolverr) |
| Browser automation | [nodriver](https://github.com/ultrafunkamsterdam/nodriver) (undetected Chrome via non-standard CDP) |
| LLM | [Anthropic API](https://docs.anthropic.com/) (Claude) |
| Email | [Resend](https://resend.com/) transactional API |
| Spreadsheet | [gspread](https://gspread.readthedocs.io/) + Google Sheets API |
| Containerisation | Docker, docker-compose, Xvfb |
| Testing | pytest (unit + integration) |

---

## Architecture

```
flatbot/
├── __main__.py          # CLI entry point — wires adapters, stores, and pipeline
├── config.py            # Env-var config with typed dataclass and defaults
├── pipeline.py          # Core loop: fetch → filter → dedup → notify
├── store.py             # Append-only seen-ID file (seen.txt)
├── matchstore.py        # Cross-platform dedup store (matches.jsonl)
├── notifier.py          # Resend email sender (HTML + plaintext fallback)
├── llm.py               # Anthropic API for email generation; template fallback
├── sheets.py            # Optional Google Sheets match log
├── logging_setup.py     # Structured stdout logging
└── adapters/
    ├── base.py          # Listing dataclass, Adapter ABC, text-detection helpers
    ├── cloudflare.py    # FlareSolverrSession — CF bypass transport layer
    ├── flatfox.py       # Plain httpx against Flatfox public JSON API
    ├── homegate.py      # nodriver headful Chrome (CF + DataDome)
    ├── immoscout.py     # nodriver headful Chrome (CF + DataDome)
    ├── newhome.py       # nodriver + in-page window.fetch (same-site CF cookie)
    └── comparis.py      # nodriver headful Chrome (DataDome only)
```

### Pipeline flow

For each adapter per cycle:

```
adapter.search()
    → filter (rooms / price / postcode)
    → seen-store check         (O(1) in-memory set)
    → match-store dedup        (normalised address key)
    → [seed] sheets.append_row + mark seen, no email
    → [normal] LLM email → send → sheets.append_row → mark seen
```

`--dry-run` short-circuits before any writes. `--seed` short-circuits before LLM / email.

---

## Bot-detection strategies

Each platform has a different protection stack; flatbot picks the right transport per site rather than hammering everything through the same approach.

| Platform | Protection | Transport |
|---|---|---|
| Flatfox | None (public JSON API) | `httpx` |
| NewHome.ch | Cloudflare Managed Challenge | nodriver (CF clears automatically); `window.fetch` for cross-origin API call |
| Comparis | DataDome only | nodriver headful Chrome |
| Homegate | Cloudflare + DataDome | FlareSolverr (CF layer) + nodriver headful Chrome (DataDome) |
| ImmoScout24 | Cloudflare + DataDome | Same as Homegate |

**Why nodriver for DataDome?** DataDome's JS fingerprinting detects headless Chrome regardless of UA patching. `nodriver` drives Chrome via a non-standard CDP variant that DataDome cannot distinguish from a real user. Headful mode (real or Xvfb virtual display) is required because headless never works.

---

## Quick start

### Prerequisites

- Python 3.11+, [uv](https://docs.astral.sh/uv/), Docker

```bash
git clone https://github.com/soljt/flat-bot.git
cd flat-bot
uv sync
cp .env.example .env   # fill in keys (see Configuration)

# Start FlareSolverr (required for Homegate and ImmoScout24)
docker compose up -d flaresolverr

# Verify FlareSolverr is reachable
curl -s http://localhost:8191/v1 \
  -H "Content-Type: application/json" \
  -d '{"cmd":"request.get","url":"https://homegate.ch/","maxTimeout":60000}' \
  | python -m json.tool | grep status
# → "status": "ok"
```

### Running

```bash
# Dry run — iterate the full pipeline, no emails, no writes, no LLM tokens
uv run python -m flatbot --one-shot --dry-run

# Seed run — log current inventory to Sheets and mark it seen; do this before going live
uv run python -m flatbot --seed

# Single real run
uv run python -m flatbot --one-shot

# Continuous loop (default: 15-min interval ± 5-min jitter)
uv run python -m flatbot
```

Two Chrome windows open briefly per cycle (Homegate + ImmoScout24/Comparis run sequentially).

---

## Running in Docker

```bash
cp .env.example .env
docker compose up -d        # builds flatbot + starts flaresolverr

docker compose logs -f flatbot

# One-off runs inside the container
docker compose run --rm flatbot --one-shot --dry-run
docker compose run --rm flatbot --seed
```

Store files (`seen.txt`, `matches.jsonl`) persist in `./data/` on the host.

### Shipping to a Raspberry Pi

```bash
# Requires: docker buildx with linux/arm64 builder, SSH access to Pi
./scripts/ship-to-pi.sh pi@raspberrypi.local
```

Builds an arm64 image, streams it to the Pi, copies `.env` and `docker-compose.yml`, and restarts the stack. Tested on Pi 4/5. Minimum recommended: 4 GB RAM.

---

## Configuration

All settings via environment variables or a `.env` file.

| Variable | Default | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | **required** | Email subject + body generation |
| `RESEND_API_KEY` | **required** | Resend transactional email API key |
| `RESEND_FROM` | **required** | Verified sender address |
| `MAILING_LIST` | **required** | Comma-separated recipient addresses |
| `MIN_ROOMS` | `5.0` | Minimum room count |
| `MIN_RENT_CHF` | `3000` | Minimum monthly rent in CHF (filters WG rooms) |
| `MAX_RENT_CHF` | `6500` | Maximum monthly rent in CHF |
| `POSTCODE_PREFIX` | `80` | Zurich city postcodes start with `80` |
| `ENABLE_FLATFOX` | `true` | Toggle Flatfox adapter |
| `ENABLE_HOMEGATE` | `true` | Toggle Homegate adapter |
| `ENABLE_IMMOSCOUT` | `true` | Toggle ImmoScout24.ch adapter |
| `ENABLE_NEWHOME` | `true` | Toggle NewHome.ch adapter |
| `ENABLE_COMPARIS` | `true` | Toggle Comparis.ch adapter |
| `POLL_INTERVAL_MIN` | `15` | Minutes between cycles |
| `POLL_JITTER_MIN` | `5` | Random ± jitter added to interval |
| `FLARESOLVERR_URL` | `http://localhost:8191/v1` | FlareSolverr endpoint |
| `FLARESOLVERR_MAX_TIMEOUT_MS` | `60000` | CF challenge timeout |
| `SEEN_STORE_PATH` | `seen.txt` | Persistent seen-IDs file |
| `MATCH_STORE_PATH` | `matches.jsonl` | Cross-platform dedup store |
| `GOOGLE_SHEETS_ID` | _(optional)_ | Sheet ID for match log |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | _(optional)_ | Path to service account key |
| `CHROME_EXECUTABLE_PATH` | _(auto)_ | Override Chromium binary path |

See `.env.example` for a ready-to-fill template. Google Sheets setup: see `SHEETS_SETUP.md`.

---

## Tests

```bash
# Unit tests (no network, no browser)
uv run pytest

# Integration tests — hit live sites; requires running FlareSolverr and Chrome
uv run pytest -m integration
```

Test coverage spans the pipeline filter logic, seen/match store guarantees, adapter output parsing, and email serialisation.

---

## Adding a new platform

1. **Create** `flatbot/adapters/yourplatform.py` and implement `class YourPlatformAdapter(Adapter)` with a `search() -> list[Listing]` method. Pick a transport based on the site's protection (see Bot-detection strategies above).

2. **Register** in `flatbot/__main__.py` behind an `ENABLE_YOURPLATFORM` env toggle.

3. **Add** the toggle to `config.py`.

4. **Seed** before going live:
   ```bash
   ENABLE_FLATFOX=false ENABLE_HOMEGATE=false ENABLE_YOURPLATFORM=true \
     uv run python -m flatbot --seed
   ```

The seed run respects the existing `MatchStore`, so a flat already notified via another platform gets the new URL appended to its Sheet row rather than generating a duplicate.

---

## Design decisions

**Why write `seen.txt` only after a confirmed send?**
A crash between send and write causes one duplicate notification which is acceptable. Writing before sending risks silently dropping a listing if the send fails, which is the worse failure mode.

**Why `uid = platform:id`?**
Flatfox and Homegate both use short numeric IDs. Namespacing prevents false deduplication if they share an ID value.

**Why nodriver over curl-cffi or Playwright?**
`curl-cffi` fakes Chrome's TLS fingerprint but runs no JS — DataDome's cookie is browser-fingerprint-bound and the challenge fails. Playwright's headless mode is detectable. nodriver passes by driving a real, unmodified Chrome binary via a non-standard protocol.

**Why skip LLM calls in dry runs?**
A dry run iterates every listing above the filter threshold, potentially 50+ items per cycle. Calling the API for all of them wastes real money with no benefit.

**Why does Flatfox use `/api/v1/pin/` instead of `/api/v1/public-listing/`?**
The OpenAPI spec for `/public-listing/` lists only `limit`, `offset`, `pk`, `status`, and `organization` with no geo or price filters. All server-side filtering flows through `/pin/`, which returns a compact list of matching IDs. Listing details are then fetched in batches by PK.

---

## License

MIT

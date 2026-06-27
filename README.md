# flatbot

Notify-first flat-match bot for Zurich. Polls Flatfox and Homegate for flats matching your criteria and emails a mailing list when a new match appears. You apply by hand — the bot never logs in, never sends messages to landlords, and never touches your dossier.

## Prerequisites

Both Flatfox and Homegate are protected by Cloudflare Managed Challenge, which blocks every plain HTTP approach. Flatbot routes all scraping through **FlareSolverr**, a local open-source proxy that clears the CF challenge in a patched browser and hands back solved cookies.

### FlareSolverr setup (Docker)

1. Install [Docker Desktop](https://www.docker.com/products/docker-desktop/) if you haven't already.

2. Start FlareSolverr with the included compose file:
   ```bash
   docker compose up -d flaresolverr
   ```

3. Verify it's working (should return `"status": "ok"`):
   ```bash
   curl -s http://localhost:8191/v1 \
     -H "Content-Type: application/json" \
     -d '{"cmd":"request.get","url":"https://flatfox.ch/","maxTimeout":60000}' \
     | python -m json.tool | grep status
   ```

4. FlareSolverr restarts automatically (`restart: unless-stopped`). Stop it with:
   ```bash
   docker compose down
   ```

The bot will fail with a clear error message if FlareSolverr isn't reachable.

## Quick start

```bash
uv sync
cp .env.example .env   # fill in your keys (see Configuration below)

# Start FlareSolverr first (see Prerequisites above)
docker compose up -d flaresolverr

# Test without sending anything
uv run python -m flatbot --one-shot --dry-run

# Real single run
uv run python -m flatbot --one-shot

# Long-lived loop (leave running)
uv run python -m flatbot
```

Logs go to stdout; redirect to a file with `uv run python -m flatbot >> flatbot.log 2>&1`.

## Configuration

All settings via environment variables or a `.env` file (see `.env.example`).

| Variable | Default | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | **required** | Used to generate email subject + body |
| `RESEND_API_KEY` | **required** | Resend transactional email API key |
| `RESEND_FROM` | **required** | Sender address — must be verified in Resend |
| `MAILING_LIST` | **required** | Comma-separated recipient addresses |
| `MIN_ROOMS` | `5.0` | Minimum room count |
| `MAX_RENT_CHF` | `6500` | Maximum monthly rent in CHF |
| `POSTCODE_PREFIX` | `80` | City of Zurich = postcodes starting with `80` |
| `POLL_INTERVAL_MIN` | `15` | Poll interval in minutes |
| `POLL_JITTER_MIN` | `5` | Random ± jitter in minutes |
| `ENABLE_FLATFOX` | `true` | Toggle Flatfox adapter |
| `ENABLE_HOMEGATE` | `true` | Toggle Homegate adapter |
| `FLARESOLVERR_URL` | `http://localhost:8191/v1` | FlareSolverr endpoint |
| `FLARESOLVERR_MAX_TIMEOUT_MS` | `60000` | Max ms FlareSolverr browser gets to clear CF |
| `GOOGLE_SHEETS_ID` | _(optional)_ | Sheet ID for match log |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | _(optional)_ | Path to service account key file |
| `SEEN_STORE_PATH` | `seen.txt` | Path to the persistent seen-IDs file |

## Resend setup

1. Sign up at https://resend.com.
2. Verify your sending domain (or use `onboarding@resend.dev` for initial testing only).
3. Create an API key and set `RESEND_API_KEY` + `RESEND_FROM` in `.env`.

## Google Sheets (optional)

See `SHEETS_SETUP.md` for step-by-step instructions. When configured, each new match is appended as a row with a blank **Human Sent Message** column you fill in manually.

## Running tests

```bash
uv run pytest
```

## Adding a new platform

1. Create `flatbot/adapters/yourplatform.py`.
2. Implement `class YourPlatformAdapter(Adapter)` with a `search() -> list[Listing]` method. Take a `FlareSolverrSession` as a constructor argument and use `session.get_json` / `session.get_html` for all requests.
3. Register it in `flatbot/__main__.py` behind an `ENABLE_YOURPLATFORM` toggle.

## Design notes

- **Idempotent:** `seen.txt` persists seen listing IDs across restarts. An ID is marked seen *only after* a successful email send — a crash between send and write may double-notify once, which is the acceptable failure direction (a missed notification is worse than a rare duplicate).
- **Fail loudly:** parse errors, login walls, CAPTCHA responses, and API failures are logged at WARNING/ERROR. The loop continues; the bot never improvises around anomalies.
- **Cloudflare-aware:** both platforms sit behind CF Managed Challenge. All requests route through a local FlareSolverr instance which solves the challenge and returns cookies. The `FlareSolverrSession` in `flatbot/adapters/cloudflare.py` handles warmup, cookie injection, and re-warmup on a stale cookie.
- **Notify-only:** no login, no auto-apply, no dossier handling, no PII, no inbound ports.

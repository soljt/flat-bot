# Flatbot — Codebase Guide

Notify-first flat-match bot for Zurich. Polls Flatfox and Homegate on a configurable interval, filters results against user criteria, and emails a mailing list for each new match. The user applies by hand — the bot never logs in, auto-applies, or touches dossiers.

---

## Architecture

```
flatbot/
├── __main__.py          # CLI entry point, wires everything together
├── config.py            # Env-var config, all settings with defaults
├── pipeline.py          # Main loop: fetch → filter → deduplicate → notify
├── store.py             # Append-only seen-ID file (seen.txt)
├── matchstore.py        # Cross-platform dedup store (matches.jsonl)
├── notifier.py          # Resend email sender (HTML + plaintext fallback)
├── llm.py               # Anthropic API for email subject/body, template fallback
├── sheets.py            # Optional Google Sheets match log
├── logging_setup.py     # Structured stdout logging
└── adapters/
    ├── base.py          # Listing dataclass, Adapter ABC, text-detection helpers
    ├── cloudflare.py    # FlareSolverrSession — CF bypass transport layer
    ├── flatfox.py       # Flatfox adapter (plain httpx, two-step pin→listing flow)
    ├── homegate.py      # Homegate adapter (nodriver headful Chrome)
    ├── immoscout.py     # ImmoScout24.ch adapter (nodriver headful Chrome)
    ├── newhome.py       # NewHome.ch adapter (nodriver + in-page fetch)
    └── comparis.py      # Comparis.ch adapter (nodriver headful Chrome)
```

### Pipeline flow (`pipeline.py`)

For each adapter in sequence:
1. `adapter.search()` → `list[Listing]`
2. `store.contains(uid)` — skip if already seen (zero cost)
3. `_passes_filter()` — rooms / price (CHF 3000–6500) / postcode gate (zero cost)
4. Compute `match_key` (normalised address or postcode+rooms+price bucket)
5. **Cross-platform dedup**: if `match_key` already in `MatchStore` (same flat seen on another platform), add other-platform link to existing Sheet row, mark seen, skip email
6. **Seed mode** (`--seed`): `sheets.append_row()` → `match_store.add()` → `store.add()` — no LLM, no email
7. **Normal run**: `generate_email()` → Anthropic API → `notifier.send()` → `sheets.append_row()` → `match_store.add()` → `store.add()`

`dry_run=True` short-circuits before step 5; no writes of any kind.
`seed_mode=True` short-circuits before step 7; no LLM tokens, no emails.

### Seen-store and match-store guarantees

- `uid = f"{platform}:{id}"` — namespaced so Flatfox and Homegate IDs can't collide
- `seen.txt` written only on confirmed send (or seed); a crash between send and write causes one harmless duplicate (acceptable — a missed notification is worse)
- `matches.jsonl` (JSONL, append-only) maps normalised listing keys to the first notification record; enables cross-platform dedup across bot cycles
- Both stores: in-memory for O(1) lookups; append-only file for crash-safe persistence
- Neither is modified by dry runs

---

## Transport layers

### Flatfox (`flatfox.py`) — two-step public API via plain httpx

Flatfox's JSON API is accessible with a plain `httpx.Client` (no FlareSolverr needed). The adapter uses a real browser `User-Agent` header but makes no CF bypass.

**Search flow** (discovered by inspecting the site's own XHR calls):
1. `GET /api/v1/pin/?east=…&west=…&north=…&south=…&min_rooms=…&max_price=…&max_count=400`
   → filtered array of `{pk, latitude, longitude, price_display, …}` (up to 400 pins)
2. `GET /api/v1/public-listing/?pk=X&pk=Y&…&limit=48&ordering=-pk` in batches of 48
   → full listing details for those PKs

The `/api/v1/public-listing/` endpoint alone has **no** geo/rooms/price filter params (confirmed in the OpenAPI spec). All filtering must go through `/api/v1/pin/` first. The old approach of paginating `public-listing` without a PIN filter fetched every listing on the platform.

If the Flatfox API starts returning 403s, it may have added CF protection — re-add `FlareSolverrSession` following the same pattern as the Homegate adapter.

**`FlareSolverrSession` (`cloudflare.py`) — Homegate only:**
- `warmup(base_url)` — solves CF challenge via FlareSolverr browser; injects `cf_clearance` cookie + UA into shared `httpx.Client`
- `get_json(url, params)` — plain HTTPS GET with CF cookies; re-warms once on 403
- `get_html(url, params)` — same but returns raw HTML text
- `fetch_via_flaresolverr(url, session=None)` — full browser render per request; slower but guaranteed; used for sites that also have DataDome
- `create_fs_session()` / `destroy_fs_session(id)` — persistent FlareSolverr browser session to reuse across multiple requests

### Homegate (`homegate.py`) — nodriver headful Chrome

Homegate has two protection layers:
- **Cloudflare Managed Challenge** on the HTML pages
- **DataDome** on the search pages (serves CAPTCHA to detected bots)

FlareSolverr clears CF but its patched Chromium fails DataDome's JS fingerprinting. `curl-cffi` (Chrome TLS impersonation) also fails because DataDome's cookie is browser-fingerprint-bound. **Headless Chrome of any kind fails DataDome.**

Solution: **`nodriver`** — controls Chrome via a non-standard protocol that DataDome cannot detect. Passes both CF and DataDome automatically in headed (visible window) mode.

**Search flow:**
1. `nodriver` opens a real Chrome window (or Xvfb virtual display in Docker)
2. Navigates to `/rent/real-estate/city-zurich/matching-list?ep=1&ac=<rooms>&al=<price>`
3. Polls `window.__INITIAL_STATE__` (Vue/Nuxt SSR state) until it appears
4. Extracts `resultList.search.fullSearch.result.{listings, pageCount}` via `page.evaluate()`
5. Navigates to subsequent pages within the same browser session (preserves DataDome cookie)
6. Closes browser after all pages

`ac` = min rooms (Anzahl Zimmer), `al` = max price, `ep` = page number (1-indexed). Each page has 20 listings; `pageCount` gives total pages; bot caps at `_MAX_PAGES = 10`.

**Docker / Raspberry Pi note:** `headless=True` fails DataDome. The Docker container starts Xvfb (virtual display) in the entrypoint script so Chrome can open a "headed" window with no physical screen. `--no-sandbox` is added automatically when `/.dockerenv` is detected (required for running as root in containers).

### ImmoScout24 (`immoscout.py`) — nodriver headful Chrome

ImmoScout24.ch is the other major Swiss rental portal (whole-flat listings, same target audience as Homegate). It has identical bot-protection layers:
- **Cloudflare Managed Challenge** on all pages
- **DataDome** on search pages

Transport is therefore identical to Homegate: `nodriver` headed Chrome.  The same Xvfb display started by `docker-entrypoint.sh` serves both adapters (they run sequentially, each opening and closing their own Chrome window).

**Search flow:**
1. `nodriver` opens a real Chrome window
2. Navigates to `/en/real-estate/rent/city-zurich?nrf=<rooms>&pf=<min_price>&pt=<max_price>&pn=<page>`
3. Polls `window.__INITIAL_STATE__` (Nuxt/Vue SSR state) until listing data appears (IS24 does not use `__NEXT_DATA__`)
4. Navigates to subsequent pages within the same browser session (preserves DataDome cookie)
5. Closes browser after all pages

`nrf` = min rooms (number of rooms from), `pf` = price from (min CHF), `pt` = price to (max CHF), `pn` = page number (1-indexed). All three filter params are applied server-side. Bot caps at `_MAX_PAGES = 10`.

**Detail URL:** `https://www.immoscout24.ch/mieten/{id}` — language-independent redirect; the `__INITIAL_STATE__` data contains no URL field so this is always constructed. Previously tried `/en/d/{id}` which 404s.

**Debugging schema changes:** if IS24 restructures their `__INITIAL_STATE__`, run with `--log-level DEBUG`. The adapter logs `action=state_dump` (top-level `pageProps` keys) and `action=first_listing_sample` (first raw listing) so you can identify the new path and update `_wait_for_state` / `_parse` in `immoscout.py`.

### NewHome.ch (`newhome.py`) — nodriver + in-page fetch

NewHome.ch (Swiss Post's property portal) is protected by a **Cloudflare Managed Challenge** — no DataDome. The adapter uses `nodriver` to navigate to the Angular search page (which clears CF automatically), then calls the JSON API from within the browser context via `window.fetch`.

**Why not FlareSolverr?** FlareSolverr clears the CF challenge on `www.newhome.ch` and injects `cf_clearance` into an `httpx` client. However, the API endpoint is on a different subdomain (`service.newhome.ch`) and CF validates that the `Sec-Fetch-Site` header is `same-site` (i.e., the request must originate from within a `www.newhome.ch` browser context). Injecting the cookie into `httpx` fails this check — only a browser already on `www.newhome.ch` can call the service subdomain.

**Search flow:**
1. `nodriver` opens Chrome and navigates to `https://www.newhome.ch/de/mieten/suchen/wohnung/ort-zuerich/liste` — CF challenge cleared automatically
2. After Angular initialises (12s wait), calls `service.newhome.ch/api/api/SearchListingRequest` from within the page via `window.fetch` (carries correct `Sec-Fetch-Site` and CF cookies)
3. Stores fetch result in `window._nh_result_N` and polls until it appears (nodriver's `evaluate()` does not await Promises)
4. Paginates via `skipCount` (0, 20, 40 …); stops when `skipCount >= totalResultCount` or `_MAX_PAGES` is reached

Key params: `location=1;2560` (Zürich municipality, covers all 80xx postcodes — semicolon must not be URL-encoded), `offerType=2` (rent), `propertyType=100` (house or apartment — **required, API rejects `propertyType=0`**), `roomsMin` / `roomsMax` / `priceMin` / `priceMax` (server-side filtering), `rowCount=20`, `languageIso=de`. `totalResultCount` gives the total matching count.

Entry fields: `immocode` (ID), `title`, `street` (includes house number), `city`, `postalCode`, `price` (gross rent CHF), `rooms`, `availabilityDate`.

**Detail URL:** `https://www.newhome.ch/en/renting/properties/apartment/apartment/city-{city-slug}/{rooms}-room/detail/{id}` — the slug segments are SEO-only decoration; the ID is the actual key. Previously used `/de/mieten/immobilien/detail/{id}` which 404s. City slug: lowercase, ü→ue, ö→oe, ä→ae, prefixed with `city-` (e.g. Zürich → `city-zuerich`). Rooms: decimal if non-integer (5.5 → `5.5-room`, 5 → `5-room`).

### Comparis.ch (`comparis.py`) — nodriver headful Chrome

Comparis.ch is a major Swiss comparison portal. Its real-estate section is a Next.js app. The site is protected by **DataDome only** (no Cloudflare), making it identical in difficulty to Homegate — `nodriver` headed Chrome is required.

**Search flow:**
1. `nodriver` opens a real Chrome window
2. Navigates to `/immobilien/result/list?requestobject=<JSON>&page=N` (page 0-indexed; omitted for page 0)
3. Accepts the CMP consent dialog on first page ("I Accept" / "Alle akzeptieren" in main document)
4. Polls `document.getElementById('__NEXT_DATA__').textContent` (Next.js SSR state) until `initialResultData.resultItems` appears
5. Navigates to subsequent pages within the same session (preserves DataDome cookie)
6. Closes browser after all pages

**Important:** The old paths `/immobilien/marktplatz/suche/mieten?RegionId=...` (dead — 404 "Ups!") and `/immobilien/marktplatz/zuerich/wohnung/mieten?p=N` (ignores all filters server-side) are both wrong. The correct path is `/immobilien/result/list?requestobject={...}`. Pagination is via `&page=N` as a **separate URL parameter** (not inside the `requestobject` JSON).

The `requestobject` JSON holds all filters: `DealType=10` (rent), `LocationSearchString="zurich"`, `RoomsFrom` (string, min rooms), `PriceFrom` / `PriceTo` (strings, CHF range), `Sort=11` (newest first). All filters are applied server-side — the response contains only matching listings.

Key data fields: `AdId` (listing ID), `Title`, `PriceValue` (numeric CHF), `EssentialInformation` (["5.5 Zimmer", "3. OG", …] — rooms in index 0), `Address` (["8006 Zürich"] — postcode + city, no street). Detail URL: `/immobilien/marktplatz/details/show/{AdId}`.

**Debugging schema changes:** run with `--log-level DEBUG` to see `action=state_dump` (pageProps keys) and `action=first_item_sample` (first raw item). Adjust `_wait_for_state` / `_parse` in `comparis.py` accordingly.

---

## Configuration (`config.py`)

All settings read from env vars or `.env`. Defaults shown below.

| Variable | Default | Notes |
|---|---|---|
| `ANTHROPIC_API_KEY` | required | Email generation |
| `RESEND_API_KEY` | required | Email sending |
| `RESEND_FROM` | required | Verified sender address |
| `MAILING_LIST` | required | Comma-separated recipients |
| `MIN_ROOMS` | `5.0` | Minimum room count |
| `MIN_RENT_CHF` | `3000` | Minimum monthly gross rent (filters WG rooms) |
| `MAX_RENT_CHF` | `6500` | Maximum monthly gross rent |
| `POSTCODE_PREFIX` | `80` | Zurich city postcodes start with `80` |
| `ENABLE_FLATFOX` | `true` | Toggle Flatfox adapter |
| `ENABLE_HOMEGATE` | `true` | Toggle Homegate adapter |
| `ENABLE_IMMOSCOUT` | `true` | Toggle ImmoScout24.ch adapter |
| `ENABLE_NEWHOME` | `true` | Toggle NewHome.ch adapter |
| `ENABLE_COMPARIS` | `true` | Toggle Comparis.ch adapter |
| `POLL_INTERVAL_MIN` | `15` | Minutes between cycles |
| `POLL_JITTER_MIN` | `5` | ± random jitter in minutes |
| `FLARESOLVERR_URL` | `http://localhost:8191/v1` | Overridden to `http://flaresolverr:8191/v1` in Docker |
| `FLARESOLVERR_MAX_TIMEOUT_MS` | `60000` | CF challenge timeout |
| `SEEN_STORE_PATH` | `seen.txt` | Set to `/app/data/seen.txt` in Docker |
| `MATCH_STORE_PATH` | `matches.jsonl` | Cross-platform dedup store; set to `/app/data/matches.jsonl` in Docker |
| `GOOGLE_SHEETS_ID` | _(empty)_ | Optional match log |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | _(empty)_ | Path to service account key |
| `CHROME_EXECUTABLE_PATH` | _(auto)_ | Override Chromium binary (set in Docker) |

---

## Running locally

```bash
uv sync

# Start FlareSolverr (required for Homegate)
docker compose up -d flaresolverr

# Dry run — no emails, no seen.txt writes, no LLM tokens
uv run python -m flatbot --one-shot --dry-run

# Seed run — log current inventory to the Sheet and mark it seen (no emails, no LLM)
# Do this BEFORE going live to avoid a flood of emails on the first real run.
uv run python -m flatbot --seed

# Single real run
uv run python -m flatbot --one-shot

# Continuous loop (15-min interval with ±5-min jitter)
uv run python -m flatbot
```

Two Chrome windows will appear briefly during each cycle — one for the Homegate scrape and one for the ImmoScout24/Comparis scrapes (nodriver adapters run sequentially).

---

## Running in Docker (local or Raspberry Pi)

```bash
cp .env.example .env   # fill in your keys

# Build and start both services
docker compose up -d

# View logs
docker compose logs -f flatbot

# Run a dry-run inside the container
docker compose run --rm flatbot --one-shot --dry-run

# Seed run (before going live): logs all current matches to the Sheet, marks them seen
docker compose run --rm flatbot --seed
```

Store files (`seen.txt`, `matches.jsonl`) are persisted in `./data/` on the host (bind-mounted to `/app/data` inside the container). This makes them easy to inspect, back up, and rsync to the Pi.

**Shipping to a Raspberry Pi:**

```bash
# Requires: docker buildx with linux/arm64 builder, SSH access to Pi
./scripts/ship-to-pi.sh pi@raspberrypi.local
```

The script builds an arm64 image, streams it to the Pi, copies `.env` and `docker-compose.yml`, and restarts the stack. Tested on Pi 4/5 (arm64). Minimum recommended: Pi 4 with 4 GB RAM (FlareSolverr + nodriver Chrome both keep browsers in memory).

---

## Adding a new platform adapter

1. **Create** `flatbot/adapters/yourplatform.py`.

2. **Implement** the adapter class:

   ```python
   from .base import Adapter, Listing, detect_no_wg, detect_price_on_request, detect_teaser_price

   class YourPlatformAdapter(Adapter):
       name = "yourplatform"

       def __init__(self, min_rooms: float, max_rent_chf: float, session) -> None:
           ...

       def search(self) -> list[Listing]:
           # Fetch, parse, return list[Listing]
           # Do NOT filter here — the pipeline handles that
           ...
   ```

3. **Choose a transport** based on the site's protection:
   - **No protection / plain API**: use `httpx` directly
   - **Cloudflare only**: use `session.get_json()` or `session.get_html()` (FlareSolverrSession)
   - **Cloudflare + DataDome** (like Homegate and ImmoScout24): use `nodriver` with Xvfb in Docker; see `homegate.py` and `immoscout.py` for the pattern
   - **Cloudflare + full-page render needed**: use `session.fetch_via_flaresolverr(url, session=fs_sid)` — slower but guaranteed

4. **Inspect the site's real API** before scraping HTML. Open DevTools → Network, apply your search filters, and look for XHR/fetch calls. Both Flatfox and Homegate turned out to have undocumented JSON endpoints that are far more reliable than HTML scraping.

5. **Register** in `flatbot/__main__.py`:
   ```python
   from .adapters.yourplatform import YourPlatformAdapter

   if cfg.enable_yourplatform:
       adapters.append(YourPlatformAdapter(min_rooms=cfg.min_rooms, max_rent_chf=cfg.max_rent_chf, session=session))
   ```

6. **Add** the toggle to `config.py`:
   ```python
   enable_yourplatform: bool = True
   # ...
   enable_yourplatform=os.getenv("ENABLE_YOURPLATFORM", "true").lower() == "true",
   ```

7. **Test** with `--dry-run --one-shot` before enabling real sends.

8. **Seed** the new platform before going live to avoid a notification flood. Use the `ENABLE_*` toggles to target only the new adapter:
   ```bash
   ENABLE_FLATFOX=false ENABLE_HOMEGATE=false ENABLE_YOURPLATFORM=true \
     uv run python -m flatbot --seed
   ```
   The seed run respects the existing `MatchStore`, so a flat already notified via another platform will get the new URL added to its Sheet row rather than generating a duplicate row.

---

## Design decisions

**Why `uid = platform:id` and not just `id`?**
Flatfox and Homegate both use short numeric IDs. Namespacing prevents false deduplication if they ever share an ID.

**Why write `seen.txt` only after a confirmed send?**
A crash between send and file-write causes one duplicate notification. That's acceptable. The alternative — writing before sending — risks silently missing a listing if the send fails.

**Why is `generate_email()` skipped in dry runs?**
Anthropic charges per token. Dry runs iterate every listing above the filter threshold, which could be 50+ items. Calling the LLM for all of them would cost real money with no benefit.

**Why is Flatfox's `/api/v1/pin/` endpoint used instead of `/api/v1/public-listing/`?**
The OpenAPI spec for `/api/v1/public-listing/` lists only `limit`, `offset`, `organization`, `pk`, `status`, and `expand` as query params — no geo/rooms/price filters. Those filters only work on `/api/v1/pin/`, which returns a compact list of matching PKs. The listing details are then fetched in batches by PK.

**Why does Homegate need a visible Chrome window?**
DataDome (Homegate's bot detection layer, separate from Cloudflare) issues CAPTCHAs to headless Chrome regardless of impersonation patches. A real Chrome with a physical or virtual (Xvfb) display passes DataDome's JS fingerprinting automatically without any user interaction.

# SF Apartment Search

A local, refreshable dashboard for hunting 2BR+ apartments in your eight target SF neighborhoods, aggregated from Craigslist, Zillow, and Padmapper.

```
sf-apartment-search/
├── refresh.py            # one-command refresh script
├── data/
│   ├── listings.json     # source of truth (append-mode)
│   └── geocode_cache.json
├── dashboard/
│   ├── index.html
│   ├── app.js
│   └── style.css
└── README.md
```

## Run it

First-time setup:

```sh
cd ~/Documents/sf-apartment-search
pip3 install -r requirements.txt    # installs feedparser
```

Then any time:

```sh
python3 refresh.py --serve
```

That starts a local server on `http://localhost:8000`, opens the dashboard in your browser, and exposes a `/api/refresh` endpoint that the dashboard's "↻ Refresh" button calls. Leave the terminal running — you never need to touch it again.

To run a one-shot pull from the terminal (no server), just `python3 refresh.py` with no flags.

**Flags:**

| Flag | What it does |
|---|---|
| `--serve [PORT]` | Run the dashboard server. Default port 8000. |
| `--notify` | Send a macOS notification when new listings score ≥ 50. |
| `--no-detail` | Skip the Zillow detail-page fetcher (saves ~30s when many listings). |
| `--llm` | Use Anthropic Claude to classify pet/outdoor/laundry/parking. Needs `ANTHROPIC_API_KEY`. |
| `--no-cl` / `--no-zw` / `--no-ap` | Skip a source. |
| `--pm` | Include Padmapper (off by default — minimal value, see below). |
| `--max-pull-age-hours 12` | Require fresher browser-pull cache files. |

## Publish to GitHub Pages (share with anyone)

One-time setup to make the dashboard accessible at a public URL like `https://your-username.github.io/sf-apt-search/`:

```sh
cd ~/Documents/sf-apartment-search

# 1. Initialize git (if not already)
git init -b main

# 2. Create the GitHub repo via gh CLI (or do this in the GitHub web UI)
gh repo create sf-apt-search --public --source=. --remote=origin --push

# 3. Enable GitHub Pages via gh
gh api -X POST /repos/{owner}/sf-apt-search/pages -f source[branch]=main -f source[path]=/
# (or set this in the repo's Settings → Pages → Source: main, root)

# 4. Verify
echo "Dashboard will be live in ~1 minute at https://$(gh api user --jq .login).github.io/sf-apt-search/"
```

After that, run `python3 refresh.py --publish` and the script will git-add the updated `listings.json`, commit, and push. GitHub Pages redeploys automatically.

The launchd plist already includes `--publish`, so once you install it and authenticate git, every 15-min refresh auto-publishes.

**Privacy note:** `listings.json` and `data/pulls/` are pushed; the cache files (`detail_cache.json`, `geocode_cache.json`, `llm_cache.json`) are gitignored. Personal preferences (notes, starred, status) live only in your browser's localStorage and are never pushed. The dashboard is fully read-only when served from GitHub Pages — the "Refresh" button gracefully falls back to showing the terminal command.

## Auto-refresh every hour

A `launchd` plist is included. To install:

```sh
cp com.bobby.sf-apt-search.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.bobby.sf-apt-search.plist
```

That kicks off a refresh every hour and pings you with a macOS notification when new listings score ≥ 50. Logs to `/tmp/sf-apt-search.log`. To uninstall, `launchctl unload` the plist and delete it.

This:
- pulls fresh listings from Craigslist (RSS), Zillow (browser), and Padmapper (browser)
- deduplicates across sources
- merges into `data/listings.json` in append mode (existing IDs get `times_seen` bumped, new IDs get `is_new_since_last_refresh = true`)
- marks any previously-active listing not seen this run as `inactive`
- appends a new entry to the refresh log
- prints a summary

Then reload the browser tab.

> **Stage 1 (current):** the source pulls are stubs that return `[]`, so running `refresh.py` on the mock data will mark everything as inactive. Confirm you're happy with the dashboard UX, then we wire up real source pulls.

### Zillow / Padmapper prerequisites

Browser pulls aren't done by `refresh.py` directly — Python can't drive your logged-in Chrome session. Instead:

1. Ask Claude (in this app, with the **Claude in Chrome** MCP available) to "refresh Zillow and Padmapper for me." Claude opens your existing logged-in tabs, scrapes the first 2 pages of each, and writes the result to `data/pulls/zillow_<timestamp>.json` and `data/pulls/padmapper_<timestamp>.json`.
2. Run `python refresh.py` — it picks up the most recent pull file per source automatically (rejects anything older than 24h by default).
3. If a captcha or "press and hold" challenge appears, the source is logged as failed and skipped.

Read-only — Claude will never click Contact, Apply, favorite, or message.

## Tune scoring weights

Open the dashboard. The **Score weights** panel (top-right, over the map) has a slider for each component. Drag a slider and every listing's score recomputes live in the browser. Your weights persist to `localStorage`.

The defaults match the spec:

| Component | Weight | Notes |
|---|---|---|
| Price fit | 20 | linear: $6,500 = 0, $4,000 or below = full weight |
| Neighborhood | 15 | flat for all 8 target neighborhoods |
| Top floor | 10 | full / half (unknown) / 0 |
| Outdoor space | 10 | confirmed only |
| Dog-friendly | 8 | full / half (unstated) / 0 |
| Quiet side street | 7 | full / partial (unknown) / 0 |
| Likely RC | 10 | scaled by 0/5/10 RC score |

Click **reset** in the weights panel to restore defaults.

## Tabs

- **Active** — passes all hard requirements (bedrooms, budget, neighborhood, laundry, parking).
- **Excluded** — in your target neighborhoods but failed laundry or parking. The reason column shows what disqualified each one — useful for spotting near-misses you might compromise on.
- **Inactive** — previously seen, no longer in any source. Useful for tracking how fast units move.
- **Refresh log** — timestamp, counts per source, errors per run.

## Filters

All filters apply to whatever tab is open. The **Hide unconfirmed parking/laundry** toggles are off by default — useful when a source omits the field but the listing might still be a fit.

## Listing actions (per row)

Click a row to open the detail panel. The action buttons:
- **Star** — visually marks the row in the table (left border).
- **Contacted** — your bookkeeping; doesn't email anyone.
- **Hide** — fades the row, hidden by default via the "Hide hidden" filter.

All three persist to `localStorage` keyed by listing ID. They survive refreshes and re-pulls.

## Add a new source

1. In `refresh.py`, write a `pull_<source>()` returning `(listings, errors)` where each listing matches the schema in `data/listings.json` (see any sample row).
2. Append it to the calls in `main()`.
3. Add it to the `sources` dict in the refresh-log entry.
4. (Optional) Add a `source-badge` color block in `dashboard/style.css`.

The dashboard auto-picks up any new `source` value — the badge will show in lowercase if no color is defined for it.

## Photos

Each listing has a `photos: string[]` field. The first entry is the table thumbnail; the full array drives the carousel in the detail panel and the popup on the map.

- **Craigslist** publishes image URLs directly in the RSS feed (`<enclosure>` tags) — `refresh.py` will copy them in.
- **Zillow & Padmapper** photos come from the same DOM the browser pull is already reading; we capture the `<img src>` of every card image, deduped.
- **Mock data** uses `picsum.photos/seed/...` placeholders for layout review only — those vanish on the first real refresh, replaced by actual listing photos.

If a listing has no photos, the table shows a small house glyph and the detail panel shows a "no photos available" placeholder.

## Where data lives

| File | What it is | When it changes |
|---|---|---|
| `data/listings.json` | Source of truth — every listing ever seen + status + scores + refresh log | Every refresh |
| `data/geocode_cache.json` | Address → lat/lng lookups (Nominatim) | Every refresh, append-only |
| `localStorage` (browser) | Score weights, starred / contacted / hidden, theme | When you interact with the dashboard |

## Known limitations

- **Zillow & Padmapper depend on Chrome.** They require a logged-in tab and the Claude in Chrome MCP. If Chrome isn't open or the session expired, those pulls return zero results and are logged as errors — Craigslist will keep working.
- **Craigslist neighborhood codes are unreliable.** We use lat/lng polygon tests, falling back to keyword matching in title/description. Listings with neither a coordinate nor a recognized name are dropped — never silently miscategorized.
- **Rent control is heuristic.** The detail panel always shows the reasoning. Override your judgment using the dashboard's filters; we don't write the override back yet.
- **No detail-page scraping by default.** We only follow a listing's detail page if it's missing parking/laundry info AND scores 60+ on the public-facing fields, to keep request volume polite.
- **Description snippets are capped at 200 chars.**
- **Read-only.** No clicking Contact / Apply / favorite / message anywhere.
- **If a source asks for login mid-pull, we stop and tell you** — no auto-handling.

## Tweaking neighborhoods

Edit `NEIGHBORHOOD_BOXES` at the top of `refresh.py`. Each entry is a list of bounding boxes; smaller / more specific neighborhoods come first so they win when areas overlap. After changing, re-run `refresh.py` and reload the dashboard. (Existing listings keep their previously-classified neighborhood until they're seen again.)

# AGENTS.md — how to update this site

Instructions for an AI agent maintaining this repo. Read all of it before changing anything.

## Routine update (when the owner says "更新一下" / "update the data")

1. **Prices (weekly):** for every slug in `data/apartments.json`, refresh `data/units/<slug>.json` — 2b2b plans, `price_min`, `as_of` (today), availability, specials. Use parallel agents, one output file each.
2. **Monthly:** `python3 -I scripts/build_data.py` (places, driving times, commute, photos) and `python3 -I scripts/crime.py`.
3. **Every 2–3 months:** refresh `data/reviews/*.json` and `data/place_reviews/*.json` for places whose `checked` date is old.
4. **Rebuild:** `python3 -I scripts/build_html.py` (stamps 最後更新).
5. **Verify:** `python3 -I scripts/phone_check.py` on the list, one apartment tab, `compare.html`, `guide.html` — zero `ERROR` lines, screenshots look right at 390 px.
6. **Publish:** `git add -A && git commit -m "Update data YYYY-MM-DD" && git push origin main`. GitHub Pages redeploys automatically in ~1 min: https://sli-23.github.io/boston-apartment-research/
7. Tell the owner what changed (new/changed prices, units that became available, anything that broke).

Ask before: adding or removing apartments, changing ranking formulas, or changing the look.

## What this is

A static apartment-hunting site for 21 Boston-area apartments, published with GitHub Pages
(`sli-23/boston-apartment-research`, branch `main`, root folder). Everything the page shows is
precomputed into JSON under `data/`, then embedded into two self-contained HTML files:

| Output | Built from | Purpose |
|---|---|---|
| `index.html` | `scripts/template.html` + `data/**` | Map + list of apartments, per-apartment tabs, filter panel |
| `guide.html` | `scripts/guide_template.html` + `data/mbta_lines.json`, `data/guide.json` | Boston guide: subway map, live weather, renter basics |
| `compare.html` | `scripts/compare_template.html` + compact rows built in `build_html.py` | Rankings (quiet / cheap / convenient) + sortable full table |

Never edit `index.html` / `guide.html` by hand — edit the templates or the data, then rebuild.

## Hard rules (the owner asked for these)

- All user-facing text in **Traditional Chinese (Taiwan usage)**, written plainly. Place/brand names stay in English.
- **Never make anything up.** Every price, rating, fee and fact needs a source URL and a date. If you can't verify it, leave it out (`null`) and say so in the relevant `*_zh` note.
- **Times:** walking only for subway/bus stops; everything else (shops, vets, hospitals, parks, Costco, office commute) is **driving** (OSRM free-flow, labeled 不含塞車).
- Commute targets are labeled **"Amazon BOS17 / BOS21"** (owner's explicit choice; keep it).
- **Photos:** only photos that clearly show the place itself. No insects/animals-in-distress, trains/buses, people close-ups, receipts, menus, or random geotagged images. Commons photos must have a title that names the place (`key_words()` filter + `JUNK` regex in `build_data.py`).
- **Parks = places to walk a dog**: fenced dog parks (`leisure=dog_park`, up to 2) + walking parks ≥ ~1 ha. Never playgrounds, tot lots, school/sports fields, courts, splash pads (`KIDS_SPORTS` regex), and never places that ban dogs (`NO_DOGS` set, `dog=no`). Park reviews focus on dog owners (`dog` block: fenced, off_leash, license_required, water, small_dog_area).
- Monthly climate chart is in **°C** (°F only as secondary).
- Every page shows **最後更新** (stamped by `build_html.py`, Boston time).
- Silver Line stays out of the per-apartment "地鐵" list (MBTA classes it as bus); it is explained on the guide page instead.
- The page must look right **on a phone**. Verify with `scripts/phone_check.py` (real mobile emulation) — headless `--window-size` cannot go below 500 px, so don't trust those screenshots for mobile.
- Default map tiles are Esri raster (works from `file://` and Pages). Don't switch to WebGL/vector maps — they rendered blank for the owner.

## Data files

| File | What | Produced by | Refresh |
|---|---|---|---|
| `data/apartments.json` | slug, name, address, lat, lng (from the owner's Google Maps list) | one-off | when the list changes |
| `data/nearby/<slug>.json` | grocery/hospital/vet/park (driving), costco, transit (walking), commute, photos | `scripts/build_data.py` | monthly |
| `data/units/<slug>.json` | official/virtual-tour links, **2b2b plans + prices**, fees, pet policy | research agent | **weekly** (prices move fast) |
| `data/reviews/<slug>.json` | resident feedback: 5 scored dimensions, pros/cons, quotes, sources | research agent | every 2–3 months |
| `data/features/<slug>.json` | balcony, in-unit W/D, AC, building amenities (powers the filter) | research agent | when a building changes amenities |
| `data/place_reviews.json`, `data/place_reviews/*.json` | Google ratings/summary/photos for nearby places, key `name|lat4,lng4` | research agent | every 2–3 months |
| `data/noise.json` | BTS 2020 transportation noise (dBA) per apartment + overlay tile URL | `scripts/noise.py` | when BTS publishes new numeric data |
| `data/crime_areas.json`, `data/crime_near.json` | FBI city rates; incidents near each building (Boston/Cambridge/Somerville) | `scripts/crime.py` | quarterly |
| `data/areas.json` | town/neighborhood polygons + colors, apartment → area | `scripts/areas.py` | rarely |
| `data/mbta_lines.json` | line shapes, stations, end-to-end times | `scripts/guide_data.py` | each MBTA schedule change (seasonal) |
| `data/guide.json` | guide text sections + NOAA climate normals | research agent | yearly, or when fares/laws change |
| `data/cache/` | raw API responses (gitignored) | all scripts | delete a file to force a refetch |

Schemas: copy the shape of an existing file (e.g. `units/hanover-alewife.json`, `reviews/hanover-alewife.json`,
`features/the-laurent.json`, `place_reviews.json`). The templates read fields defensively, but keep names identical.

## Refresh recipes

### Prices / floor plans (`data/units/<slug>.json`) — most common task
1. Find the listing: ApartmentList first — `https://www.apartmentlist.com/ma/<city>/<name-slug>` (some end in `--1`). It shows live units with prices "verified by property" and floor-plan images on `cdn.apartmentlist.com`.
2. Cross-check with the official site's floor plans and fee sheet (often a Canva/PDF link from the listing). RentCafe and Apartments.com often return 403 — try `web.archive.org`.
3. Update `plans_2b2b` (name, sqft, price string, `price_min` number, `as_of` date, availability, `image` URL verified with `curl -sI` → `image/*`), `fees`, `notes_zh` (mention move-in specials and that advertised rent may already average in free weeks).
4. Rebuild (below). The list row and hero show "2b2b 從 $X 起" from the lowest `price_min`.

### Reviews (`data/reviews/<slug>.json`)
Search: Google rating (search snippets / Google Maps place page), Reddit (r/boston, r/bostonhousing, r/CambridgeMA, r/Somerville), ApartmentList, ApartmentRatings, Apartments.com (archive), Yelp. Most of these block bots; record what was blocked in `gaps_zh`. Scores 1–5 are judgment from evidence; mention concrete noise sources (highway, rail, flight path, nightlife, construction).

### Nearby places, routes, commute, photos
```
python3 -I scripts/build_data.py [slug ...]      # all apartments if no slug
```
Sources: Overpass (falls back to Nominatim automatically when Overpass is down), OSRM foot/car (`routing.openstreetmap.de`), MBTA v3 API (20 req/min without a key — the script throttles), Wikimedia Commons, Wikidata, each place's own `og:image`.
Two processes on disjoint slug lists are fine; more will hit rate limits.

### Place ratings (`data/place_reviews/*.json`)
For each place in `data/nearby/<slug>.json`, key `f"{name}|{lat:.4f},{lng:.4f}"`. Get rating/count/top reviews/photo thumbnails from Google Maps search results (use the googleusercontent `=w600-h400-k-no` size, verify `image/*`). Write per-apartment or per-batch files into `data/place_reviews/` — never have two agents write the same file. Duplicate keys across files are OK (later file wins).

### Crime / noise / areas / MBTA lines
`python3 -I scripts/crime.py`, `scripts/noise.py`, `scripts/areas.py`, `scripts/guide_data.py`. Each documents its sources at the top. Boston violent-crime rates are scaled to FBI scope (`*_scaled_to_fbi`), see `note_zh`.

### AI-recommended apartments (「AI 推薦」 tag)
`data/ai_picks.json` holds picks chosen against the owner's criteria (quiet, safe, built/renovated ≥2015 modern interior, ≤10-min walk to subway, 2b2b + dogs + garage), each with `why_zh` / `concerns_zh` bullets backed by sources. `python3 -I scripts/add_ai_picks.py` merges them into `apartments.json` with `"ai": true` and runs areas → build_data → noise → crime. Then research `units/`, `reviews/`, `features/`, and place reviews like any other apartment. The owner asked for 5 picks; don't add more without asking.

### Convenience score (compare.html)
`2×subway walk + BOS21 drive + grocery + dog park + vet + 0.5×hospital` (minutes), min–max scaled to 0–100 across all apartments. The formula is printed on the page; if you change weights, update the text too.

### Adding a new apartment
1. Append to `data/apartments.json` (slug = lowercase name, non-alphanumerics → `-`).
2. `python3 -I scripts/areas.py` (assigns its area), `scripts/build_data.py <slug>`, `scripts/noise.py`, `scripts/crime.py`.
3. Research `units/`, `reviews/`, `features/`, `place_reviews/` for it.
4. Rebuild and verify.

## Build, verify, publish

```
python3 -I scripts/build_html.py                  # writes index.html + guide.html, stamps 最後更新 (Boston time)
python3 -I scripts/phone_check.py "" shots/p.png                      # phone: list view
python3 -I scripts/phone_check.py "hanover-alewife/units" shots/p.png # phone: a tab
python3 -I scripts/phone_check.py "guide.html#weather" shots/p.png    # phone: guide
```
Look at the screenshots (map tiles visible, nothing cut off at 390 px, no English leaking into UI text, no bad photos). Then:
```
git add -A && git commit -m "Update data YYYY-MM-DD" && git push origin main
```
Pages URL: https://sli-23.github.io/boston-apartment-research/ (deep links: `#<slug>/<tab>`, filters in `?f=balcony,wd&price=4500`, rankings `compare.html?sort=noise`).

## Gotchas

- Run Python with `-I`; scripts add their own folder to `sys.path` where needed.
- `place_reviews` keys must use 4 fixed decimals; `build_html.py` normalizes them, the page looks up with `toFixed(4)`.
- BTS noise overlay tiles exist only to zoom 12 (`maxNativeZoom: 12`).
- Office coordinates are geocoded building points (not Google Maps viewport centers) — BOS17 27 Melcher St, BOS21 55 Pier 4 Blvd.
- Initial map fits must be `animate:false`; an animated fit finishing late overwrites a deep-link zoom.
- `crime.py` has an explicit `APT_AREA` map; new slugs fall back to `data/areas.json` via `apt_area()`. Towns need an FBI ORI in `AGENCIES` (look up via `https://cde.ucr.cjis.gov/LATEST/agency/byStateAbbr/MA`).
- Parallel agents must each write their own output file and scratch folder — a shared `/tmp` script dir once corrupted another agent's results.
- Free APIs fail often (Overpass 502/504, Commons 429). Everything is cached; just rerun.

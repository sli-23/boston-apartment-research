# Boston 找房

Open `index.html` directly in a browser (works on a phone too; AirDrop the file).
Deep links: `index.html#hanover-alewife`, `index.html#hanover-alewife/reviews`.

## Rebuild
    python3 scripts/build_data.py <slug> [...]   # places, walking routes and MBTA → data/nearby/<slug>.json
    python3 scripts/build_html.py                # embed data/*.json into scripts/template.html → index.html

Reviews are hand-researched summaries in `data/reviews/<slug>.json` (same shape as hanover-alewife.json).
Sources: OpenStreetMap/Overpass, OSRM foot routing, MBTA v3 API, Esri light-gray tiles. API responses are cached in data/cache/.

## GitHub Pages
`index.html` is self-contained (data embedded), so the repo root can be published as-is:
Settings → Pages → Deploy from branch → `main` / root. `.nojekyll` skips Jekyll processing.
Note: a Pages site is public — anyone with the URL can see it.

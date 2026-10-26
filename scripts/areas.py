"""Area (town / Boston neighborhood) boundaries + per-apartment area assignment → data/areas.json.

Towns and Boston neighborhoods both come from OpenStreetMap boundary polygons via Nominatim.
Polygons are simplified so the embedded page stays small.
"""
import json
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from build_data import ROOT, fetch  # noqa: E402

BOSTON_HOODS = ["East Boston", "South Boston Waterfront", "Jamaica Plain", "Mission Hill", "Allston", "Brighton",
                "Charlestown", "Roxbury", "Fenway", "Back Bay", "Downtown", "South Boston"]
TOWNS = ["Cambridge", "Somerville", "Everett", "Chelsea", "Malden", "Melrose", "Revere", "Medford"]
# muted, distinguishable hues; zh names in Traditional Chinese
STYLE = {
    "Cambridge": ("#C0583C", "劍橋"), "Somerville": ("#D49A2A", "薩默維爾"), "Everett": ("#5B8C5A", "埃弗里特"),
    "Chelsea": ("#2F7F86", "切爾西"), "Malden": ("#4A6FA5", "莫爾登"), "Melrose": ("#7A5BA6", "梅爾羅斯"),
    "Revere": ("#B5487E", "里維爾"), "Medford": ("#4F8A7A", "梅德福"), "East Boston": ("#2B9AA0", "東波士頓"), "South Boston Waterfront": ("#3D5A99", "海港區 Seaport"),
    "Jamaica Plain": ("#8C6D3F", "牙買加平原"), "Mission Hill": ("#9C5B3C", "米慎山"), "Allston": ("#A04E6E", "奧爾斯頓"),
    "Brighton": ("#6E9A3A", "布萊頓"), "Charlestown": ("#5E6B7D", "查爾斯頓"), "Roxbury": ("#8A7A3A", "羅克斯伯里"),
    "Fenway": ("#7D8F3A", "芬威"), "Back Bay": ("#6F5A8C", "後灣"), "Downtown": ("#555F73", "市中心"), "South Boston": ("#3F7FA5", "南波士頓"),
}
FALLBACK = ("#6B6B6B", None)


def simplify(ring, tol=0.0004):
    """Ramer–Douglas–Peucker on a lon/lat ring."""
    if len(ring) < 4:
        return ring

    def rdp(pts):
        (x1, y1), (x2, y2) = pts[0], pts[-1]
        dx, dy = x2 - x1, y2 - y1
        norm = (dx * dx + dy * dy) ** 0.5 or 1e-12
        idx, dmax = 0, 0.0
        for i in range(1, len(pts) - 1):
            d = abs(dy * pts[i][0] - dx * pts[i][1] + x2 * y1 - y2 * x1) / norm
            if d > dmax:
                idx, dmax = i, d
        if dmax > tol:
            return rdp(pts[: idx + 1])[:-1] + rdp(pts[idx:])
        return [pts[0], pts[-1]]

    # a closed ring starts and ends on the same point, so split it at the farthest vertex first
    x0, y0 = ring[0]
    far = max(range(len(ring)), key=lambda i: (ring[i][0] - x0) ** 2 + (ring[i][1] - y0) ** 2)
    out = rdp(ring[: far + 1])[:-1] + rdp(ring[far:])
    return [[round(x, 4), round(y, 4)] for x, y in out]


def polygons(geom):
    """Normalize Polygon/MultiPolygon to a list of outer rings (holes dropped)."""
    if geom["type"] == "Polygon":
        return [geom["coordinates"][0]]
    if geom["type"] == "MultiPolygon":
        return [p[0] for p in geom["coordinates"]]
    return []


def inside(pt, ring):
    x, y, hit = pt[0], pt[1], False
    for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]):
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            hit = not hit
    return hit


def town_polygon(name, hood=False):
    params = {"q": f"{name}, Boston, Massachusetts"} if hood else {"city": name, "state": "Massachusetts", "country": "USA"}
    q = urllib.parse.urlencode({**params, "format": "jsonv2", "polygon_geojson": 1, "polygon_threshold": 0.0003, "limit": 3})
    for r in fetch(f"https://nominatim.openstreetmap.org/search?{q}", delay=1.1):
        if r.get("geojson", {}).get("type") in ("Polygon", "MultiPolygon"):
            return polygons(r["geojson"])
    return []


def main():
    raw = {}
    for h in BOSTON_HOODS:  # neighborhoods first so they win over the Boston city polygon
        raw[h] = town_polygon(h, hood=True)
        print(f"{h}: {len(raw[h])} rings")
    for t in TOWNS:
        raw[t] = town_polygon(t)
        print(f"{t}: {len(raw[t])} rings")

    apts = json.loads((ROOT / "data" / "apartments.json").read_text())
    assign = {}
    for a in apts:
        pt = (a["lng"], a["lat"])
        hit = next((n for n, rings in raw.items() if any(inside(pt, r) for r in rings)), None)
        assign[a["slug"]] = hit or "Other"
        print(f"  {a['name']:45s} → {assign[a['slug']]}")

    used = sorted(set(assign.values()))
    areas = {}
    for n in used:
        color, zh = STYLE.get(n, FALLBACK)
        areas[n] = {"color": color, "zh": zh or n, "rings": [simplify(r) for r in raw.get(n, [])]}
    out = {"areas": areas, "apartments": assign}
    (ROOT / "data" / "areas.json").write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")))
    print("areas.json:", {n: sum(len(r) for r in a["rings"]) for n, a in areas.items()})


if __name__ == "__main__":
    main()

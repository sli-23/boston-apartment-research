"""MBTA line shapes, stations and end-to-end times for the guide page → data/mbta_lines.json."""
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from build_data import ROOT, fetch_mbta  # noqa: E402
from commute import _morning_trips  # noqa: E402

RAPID = ["Red", "Orange", "Blue", "Green-B", "Green-C", "Green-D", "Green-E", "Mattapan"]
SILVER = ["741", "742", "743", "751", "749"]


def decode(poly):
    """Google encoded polyline → [[lat, lng], ...]."""
    out, i, lat, lng = [], 0, 0, 0
    while i < len(poly):
        for k in range(2):
            shift = result = 0
            while True:
                b = ord(poly[i]) - 63
                i += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            d = ~(result >> 1) if result & 1 else result >> 1
            if k == 0:
                lat += d
            else:
                lng += d
        out.append([round(lat / 1e5, 5), round(lng / 1e5, 5)])
    return out


def thin(pts, step=3):
    return pts[::step] + ([pts[-1]] if (len(pts) - 1) % step else [])


def main():
    routes = {r["id"]: r["attributes"] for r in fetch_mbta("/routes", **{"filter[id]": ",".join(RAPID + SILVER)})}
    nearby = {p.stem: json.loads(p.read_text()) for p in (ROOT / "data" / "nearby").glob("*.json")}
    apts = {a["slug"]: a for a in json.loads((ROOT / "data" / "apartments.json").read_text())}
    out = {"lines": [], "stations": {}}
    for rid in RAPID + SILVER:
        a = routes[rid]
        shapes = fetch_mbta("/shapes", **{"filter[route]": rid})
        canon = [s for s in shapes if s["id"].startswith("canonical")] or shapes
        # keep one shape per distinct branch (direction 0 canonical shapes cover each branch once)
        polys, seen = [], set()
        for s in sorted(canon, key=lambda s: -len(s["attributes"]["polyline"])):
            pts = decode(s["attributes"]["polyline"])
            key = (round(pts[0][0], 3), round(pts[-1][0], 3))
            if key in seen or (key[1], key[0]) in seen:
                continue
            seen.add(key)
            polys.append(thin(pts))
        # end-to-end morning ride time per branch, from the weekday timetable
        trips = []
        for seq in _morning_trips(rid, 0):
            mins = (datetime.fromisoformat(seq[-1]["arr"]) - datetime.fromisoformat(seq[0]["dep"])).total_seconds() / 60
            trips.append({"from": seq[0]["name"], "to": seq[-1]["name"], "stops": len(seq), "minutes": round(mins)})
            if rid in RAPID:
                for st in seq:
                    out["stations"].setdefault(st["id"], {"name": st["name"], "lat": st["lat"], "lng": st["lng"], "lines": []})
                    if rid not in out["stations"][st["id"]]["lines"]:
                        out["stations"][st["id"]]["lines"].append(rid)
        near = sorted({slug for slug, n in nearby.items() for s in n["transit"]["rapid"]
                       if any(l["id"] == rid for l in s["lines"])})
        out["lines"].append({"id": rid, "name": a["long_name"], "short": a["short_name"], "kind": "silver" if rid in SILVER else "rapid",
                             "color": "#" + a["color"], "text": "#" + a["text_color"], "ends": a["direction_destinations"],
                             "shapes": polys, "trips": trips[:3], "apartments": [apts[s]["name"] for s in near if s in apts]})
        print(rid, len(polys), "shapes", trips[:2], "near:", out["lines"][-1]["apartments"])
    (ROOT / "data" / "mbta_lines.json").write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")))
    print("stations", len(out["stations"]), "bytes", (ROOT / "data" / "mbta_lines.json").stat().st_size)


if __name__ == "__main__":
    main()

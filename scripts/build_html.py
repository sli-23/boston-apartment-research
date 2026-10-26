"""Embed data/*.json into scripts/template.html → index.html."""
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

UPDATED = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M")  # Boston time

ROOT = Path(__file__).resolve().parent.parent
data = {
    "apartments": json.loads((ROOT / "data" / "apartments.json").read_text()),
    "nearby": {p.stem: json.loads(p.read_text()) for p in sorted((ROOT / "data" / "nearby").glob("*.json"))},
    "reviews": {p.stem: json.loads(p.read_text()) for p in sorted((ROOT / "data" / "reviews").glob("*.json"))},
    "units": {p.stem: json.loads(p.read_text()) for p in sorted((ROOT / "data" / "units").glob("*.json"))},
    "features": {p.stem: json.loads(p.read_text()) for p in sorted((ROOT / "data" / "features").glob("*.json"))},
    **{k: json.loads((ROOT / "data" / f"{k}.json").read_text()) if (ROOT / "data" / f"{k}.json").exists() else None
       for k in ("areas", "noise", "crime_areas", "crime_near")},
}
# place reviews keyed "name|lat,lng" — normalize to 4 fixed decimals so JS toFixed(4) matches
data["place_reviews"] = {}
files = [ROOT / "data" / "place_reviews.json", *sorted((ROOT / "data" / "place_reviews").glob("*.json"))]
for pr in (f for f in files if f.exists()):
    for k, v in json.loads(pr.read_text()).items():
        name, _, ll = k.rpartition("|")
        lat, lng = (float(x) for x in ll.split(","))
        data["place_reviews"][f"{name}|{lat:.4f},{lng:.4f}"] = v
blob = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
html = (ROOT / "scripts" / "template.html").read_text().replace("/*__DATA__*/null", blob).replace("__UPDATED__", UPDATED)
(ROOT / "index.html").write_text(html)
print(f"index.html: {len(data['apartments'])} apartments, {len(data['nearby'])} with nearby data, {len(data['reviews'])} with reviews, {len(data['units'])} with units")

# guide.html: subway map, weather, renter basics
import sys
sys.path.insert(0, str(ROOT / "scripts"))
from build_data import OFFICES  # noqa: E402

lines_p, guide_p = ROOT / "data" / "mbta_lines.json", ROOT / "data" / "guide.json"
G = {**(json.loads(lines_p.read_text()) if lines_p.exists() else {"lines": [], "stations": {}}),
     "areas": data["areas"], "apartments": data["apartments"], "offices": OFFICES,
     "guide": json.loads(guide_p.read_text()) if guide_p.exists() else None,
     "crime": data["crime_areas"],
     "crime_apts": {s_: ((data["crime_near"] or {}).get("apartments", {}).get(s_) or {}).get("area")
                    or (data["areas"] or {}).get("apartments", {}).get(s_) for s_ in (a["slug"] for a in data["apartments"])},
     **{k: (json.loads((ROOT / "data" / f).read_text()) if (ROOT / "data" / f).exists() else None)
        for k, f in (("tech", "tech_companies.json"), ("tax", "guide_tax.json"), ("dog", "guide_dog.json"))}}
gblob = json.dumps(G, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
(ROOT / "guide.html").write_text((ROOT / "scripts" / "guide_template.html").read_text().replace("/*__GUIDE__*/null", gblob).replace("__UPDATED__", UPDATED))
print(f"guide.html: {len(G['lines'])} lines, guide text {'yes' if G['guide'] else 'pending'}")

# compare.html: ranking + full comparison table (compact rows, not the full data)
import re as _re
def _num(v):
    m = _re.search(r"\$?([\d,]{2,6})", v or "")
    return int(m.group(1).replace(",", "")) if m else None
rows = []
for a in data["apartments"]:
    s_ = a["slug"]; n = data["nearby"].get(s_) or {}; u = data["units"].get(s_) or {}; rv = data["reviews"].get(s_) or {}
    f = data["features"].get(s_) or {}; nz = (data["noise"] or {}).get("apartments", {}).get(s_) or {}
    cn = (data["crime_near"] or {}).get("apartments", {}).get(s_) or {}
    area = (data["areas"] or {}).get("apartments", {}).get(s_)
    ar = (data["areas"] or {}).get("areas", {}).get(area, {})
    mins = lambda c: min((p["route"]["minutes"] for p in n.get(c, [])), default=None)
    rapid = (n.get("transit") or {}).get("rapid") or []
    dims = {d["key"]: d.get("score") for d in rv.get("dimensions", [])}
    prices = [p["price_min"] for p in u.get("plans_2b2b", []) if p.get("price_min")]
    rows.append({"slug": s_, "name": a["name"], "area": ar.get("zh", area or ""), "color": ar.get("color", "#8E897D"),
                 "ai": bool(a.get("ai")),
                 "price": min(prices) if prices else None,
                 "noise": (nz.get("combined_db") if nz.get("combined_db") is not None else (45.0 if nz else None)),
                 "noise50": nz.get("max_50m_db"),
                 "subway": rapid[0]["route"]["minutes"] if rapid else None,
                 "line": rapid[0]["lines"][0]["name"] if rapid and rapid[0]["lines"] else None,
                 "lineColor": rapid[0]["lines"][0]["color"] if rapid and rapid[0]["lines"] else None,
                 "bos17": ((n.get("commute") or {}).get("BOS17") or {}).get("drive", {}).get("minutes"),
                 "bos21": ((n.get("commute") or {}).get("BOS21") or {}).get("drive", {}).get("minutes"),
                 "transit21": ((n.get("commute") or {}).get("BOS21") or {}).get("transit", {}).get("total"),
                 "grocery": mins("grocery"), "park": mins("park"), "vet": mins("vet"), "hospital": mins("hospital"),
                 "quiet": dims.get("noise"), "safe": dims.get("safety"),
                 "crime": cn.get("property") if cn.get("available") else None,
                 "balcony": (f.get("unit") or {}).get("balcony"), "wd": (f.get("unit") or {}).get("in_unit_wd"),
                 "parkingFee": _num((u.get("fees") or {}).get("parking_garage")),
                 **(lambda A: {"areaViolent": A.get("violent_per_1k_scaled_to_fbi", A.get("violent_per_1k")),
                               "areaProperty": A.get("property_per_1k")})(
                     ((data["crime_areas"] or {}).get("areas", {}).get(cn.get("area") or area)) or {})})
cblob = json.dumps({"rows": rows}, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
(ROOT / "compare.html").write_text((ROOT / "scripts" / "compare_template.html").read_text()
                                   .replace("/*__COMPARE__*/null", cblob).replace("__UPDATED__", UPDATED))
print(f"compare.html: {len(rows)} rows")

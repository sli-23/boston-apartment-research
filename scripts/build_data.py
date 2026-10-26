"""Precompute nearby places (driving), MBTA stations (walking), commute, Costco and photos per apartment.

Usage: python3 scripts/build_data.py [slug ...]   (default: all apartments)
Writes data/nearby/<slug>.json. Raw API responses are cached in data/cache/.
"""
import hashlib
import json
import math
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "data" / "cache"
OUT = ROOT / "data" / "nearby"
UA = "boston-apartments-personal/1.0"

OVERPASS = ["https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter"]
NOMINATIM = "https://nominatim.openstreetmap.org/search"
# Nominatim keyword search per category, used only when every Overpass server is down.
NOMINATIM_TERMS = {
    "grocery": (["supermarket", "grocery", "Star Market", "Market Basket", "H Mart", "Stop & Shop"], {"shop": {"supermarket", "greengrocer", "convenience"}}),
    "hospital": (["hospital", "urgent care"], {"amenity": {"hospital", "clinic"}, "healthcare": None}),
    "vet": (["veterinary", "animal hospital", "vet"], {"amenity": {"veterinary"}}),
    "park": (["dog park", "off leash dog", "park", "reservation", "conservation area", "greenway"],
             {"leisure": {"park", "nature_reserve", "dog_park", "common"}, "boundary": {"protected_area"}}),
    "costco": (["Costco"], {"shop": None}),
}
OSRM_FOOT = "https://routing.openstreetmap.de/routed-foot/route/v1/foot"
OSRM_CAR = "https://routing.openstreetmap.de/routed-car/route/v1/driving"
COMMONS = "https://commons.wikimedia.org/w/api.php"
OFFICES = {
    "BOS17": {"name": "Amazon BOS17", "address": "27 Melcher St, Boston, MA 02210", "lat": 42.3498363, "lng": -71.0508339},
    "BOS21": {"name": "Amazon BOS21", "address": "55 Pier 4 Blvd, Boston, MA 02210", "lat": 42.3500349, "lng": -71.0451397},
}
MBTA = "https://api-v3.mbta.com"

# category → (overpass filters unioned, search radii in m, how many to keep)
CATEGORIES = {
    "grocery": (['["shop"~"^(supermarket|greengrocer)$"]["name"]'], (2500, 4000), 6),
    "hospital": (['["amenity"="hospital"]["name"]',
                  '["amenity"="clinic"]["name"~"Urgent|CareWell|AFC|MinuteClinic|Walk-In",i]'], (4000, 7000), 3),
    "vet": (['["amenity"="veterinary"]["name"]'], (3000, 5000), 3),
    # dog parks + walking parks (no playgrounds / sports fields); filtered further by KIDS_SPORTS + size below
    "park": (['["leisure"="dog_park"]', '["leisure"~"^(park|nature_reserve)$"]["name"]',
              '["leisure"="common"]["name"]', '["boundary"="protected_area"]["name"]["access"!="private"]'], (3000, 5000), 4),
}
CANDIDATES = 9
KIDS_SPORTS = re.compile(r"playground|tot ?lot|school|\bfield\b|fields\b|court|stadium|little league|pool|splash|spray|tennis|"
                         r"basketball|skate|ball ?field|diamond|elementary|academy|daycare|children|kids|tot\b|track|rink|"
                         r"historic district|parkway|cemetery", re.I)
NO_DOGS = {"Belle Isle Marsh Reservation"}  # dogs prohibited (DCR); found via reviews 2026-10-26
MIN_WALK_PARK_HA = 1.0  # walking parks must be at least ~1 hectare (a short loop)


def fetch(url, data=None, tries=4, delay=1.0, timeout=60):
    key = hashlib.sha1((url + (data or "")).encode()).hexdigest()
    path = CACHE / f"{key}.json"
    if path.exists():
        return json.loads(path.read_text())
    body = data.encode() if data else None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, data=body, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                payload = json.loads(r.read())
            path.write_text(json.dumps(payload))
            time.sleep(delay)  # be polite to free public APIs
            return payload
        except Exception as e:  # noqa: BLE001
            print(f"  retry {attempt + 1}/{tries} {url[:70]}: {e}", file=sys.stderr)
            time.sleep((20 if "429" in str(e) else 3) * (attempt + 1))
    raise RuntimeError(f"failed: {url}")


def haversine(a, b):
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 6371000 * 2 * math.asin(math.sqrt(h))


def overpass(lat, lng, filters, radius):
    union = "".join(f"nwr(around:{radius},{lat},{lng}){f};" for f in filters)
    q = f"[out:json][timeout:50];({union});out center tags bb;"
    last = None
    for ep in OVERPASS:
        try:
            return fetch(ep, "data=" + urllib.parse.quote(q), tries=1, timeout=30)["elements"]
        except RuntimeError as e:
            last = e
    raise last


def nominatim(lat, lng, cat, radius):
    """Overpass-shaped elements from Nominatim keyword search inside a bounding box."""
    terms, allowed = NOMINATIM_TERMS[cat]
    dlat, dlng = radius / 111_000, radius / (111_000 * math.cos(math.radians(lat)))
    box = f"{lng - dlng},{lat + dlat},{lng + dlng},{lat - dlat}"
    out, seen = [], set()
    for term in terms:
        try:
            res = fetch(f"{NOMINATIM}?" + urllib.parse.urlencode({"format": "jsonv2", "q": term, "viewbox": box, "bounded": 1,
                                                              "limit": 40, "extratags": 1, "addressdetails": 1}), delay=1.1)
        except RuntimeError:  # one rate-limited search term shouldn't sink the whole category
            continue
        for x in res:
            ok = x["category"] in allowed and (allowed[x["category"]] is None or x["type"] in allowed[x["category"]])
            if not ok or not x.get("name") or x["osm_id"] in seen:
                continue
            if cat == "costco" and not x["name"].startswith("Costco"):
                continue
            seen.add(x["osm_id"])
            a, t = x.get("address") or {}, x.get("extratags") or {}
            tags = {"name": x["name"], "addr:housenumber": a.get("house_number"), "addr:street": a.get("road"),
                    "addr:city": a.get("city") or a.get("town"), **t}
            bb = [float(v) for v in x.get("boundingbox", [])] or None  # [south, north, west, east]
            tags["leisure"] = tags.get("leisure") or (x["type"] if x["category"] == "leisure" else None)
            out.append({"lat": float(x["lat"]), "lon": float(x["lon"]), "tags": {k: v for k, v in tags.items() if v},
                        **({"bounds": {"minlat": bb[0], "maxlat": bb[1], "minlon": bb[2], "maxlon": bb[3]}} if bb else {})})
    return out


OVERPASS_DOWN = False  # circuit breaker: after one total failure, skip Overpass for the rest of the run


def find(lat, lng, filters, radius, cat):
    global OVERPASS_DOWN
    if not OVERPASS_DOWN:
        try:
            return overpass(lat, lng, filters, radius)
        except RuntimeError:
            OVERPASS_DOWN = True
    print(f"  overpass down → nominatim for {cat}", file=sys.stderr)
    return nominatim(lat, lng, cat, radius)


def walk(a, b):
    url = f"{OSRM_FOOT}/{a[1]},{a[0]};{b[1]},{b[0]}?overview=full&geometries=geojson"
    r = fetch(url)["routes"][0]
    # Use a 4.8 km/h walking pace; OSRM foot durations are similar but rounder.
    return {
        "meters": round(r["distance"]),
        "minutes": max(1, round(r["distance"] / 80)),
        "line": [[round(y, 6), round(x, 6)] for x, y in r["geometry"]["coordinates"]],
    }


def drive(a, b):
    """Free-flow driving time (no traffic) from OSRM."""
    url = f"{OSRM_CAR}/{a[1]},{a[0]};{b[1]},{b[0]}?overview=full&geometries=geojson"
    r = fetch(url)["routes"][0]
    return {
        "mode": "drive",
        "meters": round(r["distance"]),
        "minutes": max(1, round(r["duration"] / 60)),
        "line": [[round(y, 5), round(x, 5)] for x, y in r["geometry"]["coordinates"]],
    }


GENERIC = {"park", "parks", "playground", "field", "hospital", "medical", "center", "centre", "clinic", "care", "urgent",
           "veterinary", "vet", "animal", "market", "supermarket", "foods", "food", "the", "and", "of", "at", "square",
           "boston", "cambridge", "somerville", "reverend", "saint", "memorial", "community", "street", "avenue"}
JUNK = re.compile(r"map|logo|diagram|plan|sign|\bbus\b|busway|locomotive|boxcar|freight|\.agr\b|\bB&M\b|MBTA \d|route \d|"
                  r"insect|mantis|mantclose|spider|snake|bug|beetle|wasp|moth|fly\b|rat\b|mouse|bird|duck|merganser|turtle|squirrel|"
                  r"fungus|mushroom|amanita|dead|crash|fire\b|accident|abandoned|graffiti|trash|grave|cemetery|tomb|"
                  r"construction|demolition|protest|police", re.I)


def key_words(name):
    return [w for w in re.findall(r"[A-Za-z']{4,}", name.lower()) if w not in GENERIC]


def _commons(params, limit, must=None):
    base = {"action": "query", "format": "json", "prop": "imageinfo", "iiprop": "url|extmetadata", "iiurlwidth": 640}
    pages = fetch(f"{COMMONS}?" + urllib.parse.urlencode({**base, **params})).get("query", {}).get("pages", {})
    out = []
    for p in sorted(pages.values(), key=lambda p: p.get("index", 0)):
        title = p["title"]
        if not title.lower().endswith((".jpg", ".jpeg", ".png")) or JUNK.search(title):
            continue
        if must and not any(w in title.lower() for w in must):
            continue
        ii = p["imageinfo"][0]
        meta = ii.get("extmetadata", {})
        artist = re.sub(r"<[^>]+>", "", meta.get("Artist", {}).get("value", "")).strip()
        out.append({"thumb": ii["thumburl"], "page": ii["descriptionurl"],
                    "title": re.sub(r"^File:|\.\w+$", "", title),
                    "artist": artist[:60], "license": meta.get("LicenseShortName", {}).get("value", "")})
        if len(out) >= limit:
            break
    return out


def photos(lat, lng, radius, limit, must=None):
    """Geotagged Wikimedia Commons photos near a point; `must` = words the file title has to contain."""
    return _commons({"generator": "geosearch", "ggscoord": f"{lat}|{lng}", "ggsradius": radius,
                     "ggsnamespace": 6, "ggslimit": 40}, limit, must)


def named_photos(name, city, limit):
    """Commons files whose title mentions the place's distinctive name words."""
    words = key_words(name)
    if not words:
        return []
    return _commons({"generator": "search", "gsrsearch": f'"{name}" {city or ""}'.strip(),
                     "gsrnamespace": 6, "gsrlimit": 15}, limit, words)


def wikidata_photo(qid):
    if not qid:
        return []
    ent = fetch(f"https://www.wikidata.org/wiki/Special:EntityData/{qid}.json")["entities"]
    claims = next(iter(ent.values())).get("claims", {}).get("P18", [])
    if not claims:
        return []
    fname = claims[0]["mainsnak"]["datavalue"]["value"]
    return _commons({"titles": "File:" + fname}, 1)


def area_ha(b):
    """Rough area from a bounding box (ha); None when unknown."""
    if not b:
        return None
    h = (b["maxlat"] - b["minlat"]) * 111_000
    w = (b["maxlon"] - b["minlon"]) * 111_000 * math.cos(math.radians((b["maxlat"] + b["minlat"]) / 2))
    return round(h * w * 0.6 / 10_000, 1)  # bbox overestimates a park's footprint; 0.6 is a typical fill ratio


def site_image(url):
    """The og:image a business's own site shows in link previews (cached as JSON)."""
    if not url:
        return None
    if not url.startswith("http"):
        url = "https://" + url
    path = CACHE / ("og_" + hashlib.sha1(url.encode()).hexdigest() + ".json")
    if path.exists():
        return json.loads(path.read_text())["img"]
    img = None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Macintosh) boston-apartments/1.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            html = r.read(400_000).decode("utf-8", "ignore")
        m = (re.search(r'<meta[^>]+property=["\']og:image(?::secure_url)?["\'][^>]+content=["\']([^"\']+)', html, re.I)
             or re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image', html, re.I))
        if m:
            img = urllib.parse.urljoin(url, m.group(1).replace("&amp;", "&"))
    except Exception as e:  # noqa: BLE001 — many small-business sites are flaky
        print(f"  og:image skip {url}: {e}", file=sys.stderr)
    path.write_text(json.dumps({"img": img}))
    return img


def places(apt, cat):
    origin = (apt["lat"], apt["lng"])
    filters, radii, keep = CATEGORIES[cat]
    for radius in radii:
        els = find(*origin, filters, radius, cat)
        if len(els) >= keep:
            break
    seen, items = set(), []
    for e in els:
        if "lat" not in e and "center" not in e:  # some relations come back without a center
            continue
        pos = (e["lat"], e["lon"]) if "lat" in e else (e["center"]["lat"], e["center"]["lon"])
        t = e["tags"]
        name = t.get("name")
        extra = {}
        if cat == "park":
            dog = t.get("leisure") == "dog_park"
            if t.get("dog") == "no" or name in NO_DOGS:
                continue
            if not dog and (not name or KIDS_SPORTS.search(name)):
                continue
            ha = area_ha(e.get("bounds"))
            if not dog and ha is not None and ha < MIN_WALK_PARK_HA:
                continue
            name = name or "狗公園（未命名）"
            extra = {"kind": "dog" if dog else "walk", "area_ha": ha, "dog_rule": t.get("dog"),
                     "fenced": t.get("barrier") == "fence" or t.get("fence") == "yes" or None}
        elif not name:
            continue
        if name in seen:
            continue
        seen.add(name)
        items.append({"name": name, "lat": pos[0], "lng": pos[1], "crow": haversine(origin, pos), "tags": t, **extra})
    if cat == "park":  # give unnamed dog parks the name of the park they sit in
        for it in items:
            if it["name"] == "狗公園（未命名）":
                host = min((x for x in items if x.get("kind") == "walk"), default=None,
                           key=lambda x: haversine((it["lat"], it["lng"]), (x["lat"], x["lng"])))
                if host and haversine((it["lat"], it["lng"]), (host["lat"], host["lng"])) < 500:
                    it["name"] = f"{host['name']} 狗公園"
    items.sort(key=lambda x: x["crow"])
    out = []
    if cat == "park":  # make sure dog parks get routed even if a bit farther than the walking parks
        items = [x for x in items if x.get("kind") == "dog"][:3] + [x for x in items if x.get("kind") != "dog"]
    for it in items[:CANDIDATES]:
        t = it.pop("tags")
        it.pop("crow")
        addr = " ".join(filter(None, [t.get("addr:housenumber"), t.get("addr:street")])) if t.get("addr:street") else None
        it.update(address=addr or None, brand=t.get("brand"), hours=t.get("opening_hours"),
                  website=t.get("website") or t.get("contact:website"), phone=t.get("phone"),
                  wikidata=t.get("wikidata"), city=t.get("addr:city"))
        it["route"] = drive(origin, (it["lat"], it["lng"]))
        out.append(it)
    out.sort(key=lambda x: (x["route"]["minutes"], x["route"]["meters"]))
    if cat == "park":
        dogs = [x for x in out if x["kind"] == "dog"][:2]
        walks = [x for x in out if x["kind"] == "walk"][: keep - len(dogs)]
        out = sorted(dogs + walks, key=lambda x: x["route"]["minutes"])
    out = out[:keep]
    for it in out:
        pics = []
        og = site_image(it.get("website"))
        if og:
            pics.append({"thumb": og, "page": it["website"] if it["website"].startswith("http") else "https://" + it["website"],
                         "title": it["name"] + " 官網", "artist": it["name"], "license": "官網圖片"})
        pics += wikidata_photo(it.pop("wikidata", None))
        pics += named_photos(it["name"], it.pop("city", None), 3)
        if not pics and cat == "park":  # parks: nearby geotagged files whose title names the park
            pics += photos(it["lat"], it["lng"], 400, 3, key_words(it["name"]) or ["\0"])
        seen, it["photos"] = set(), []
        for ph in pics:
            if ph["thumb"] not in seen:
                seen.add(ph["thumb"])
                it["photos"].append(ph)
        it["photos"] = it["photos"][:3]
        it["photo"] = it["photos"][0] if it["photos"] else None
    return out


def costco(apt):
    origin = (apt["lat"], apt["lng"])
    els = find(*origin, ['["shop"]["name"~"^Costco"]'], 40000, "costco")
    stores = []
    for e in els:
        if "lat" not in e and "center" not in e:  # some relations come back without a center
            continue
        pos = (e["lat"], e["lon"]) if "lat" in e else (e["center"]["lat"], e["center"]["lon"])
        t = e["tags"]
        stores.append({"name": t["name"], "lat": pos[0], "lng": pos[1], "hours": t.get("opening_hours"),
                       "address": " ".join(filter(None, [t.get("addr:housenumber"), t.get("addr:street"), t.get("addr:city")])) or None,
                       "crow": haversine(origin, pos)})
    stores.sort(key=lambda x: x["crow"])
    for s_ in stores[:3]:
        s_.pop("crow")
        s_["route"] = drive(origin, (s_["lat"], s_["lng"]))
    return sorted(stores[:3], key=lambda x: x["route"]["minutes"])[:2]


def commute(apt):
    sys.path.insert(0, str(Path(__file__).parent))  # python -I drops the script dir
    from commute import build_graph, plan
    graph = build_graph()
    origin = (apt["lat"], apt["lng"])
    out = {}
    for code, office in OFFICES.items():
        dest = (office["lat"], office["lng"])
        out[code] = {**office, "drive": drive(origin, dest), "transit": plan(origin, dest, graph)}
    return out


def fetch_mbta(path, full=False, **params):
    # MBTA v3 allows ~20 requests/minute without an API key.
    res = fetch(f"{MBTA}{path}?" + urllib.parse.urlencode(params), delay=3.2)
    return res if full else res["data"]


mbta = fetch_mbta


def transit(apt):
    origin = (apt["lat"], apt["lng"])
    result = {"rapid": [], "commuter": [], "bus": []}
    groups = {"rapid": "0,1", "commuter": "2", "bus": "3"}
    radius = {"rapid": 0.02, "commuter": 0.025, "bus": 0.006}  # MBTA radius is in degrees
    for group, rtype in groups.items():
        stops = mbta("/stops", **{"filter[latitude]": apt["lat"], "filter[longitude]": apt["lng"],
                                  "filter[radius]": radius[group], "filter[route_type]": rtype})
        seen, chosen = set(), []
        for s in sorted(stops, key=lambda s: haversine(origin, (s["attributes"]["latitude"], s["attributes"]["longitude"]))):
            parent = (s["relationships"].get("parent_station") or {}).get("data")
            sid = parent["id"] if parent else s["id"]
            name = s["attributes"]["name"]
            if sid in seen or name in seen:
                continue
            seen.update({sid, name})
            chosen.append((sid, s))
            if len(chosen) >= (2 if group == "rapid" else 1 if group == "commuter" else 3):
                break
        for sid, s in chosen:
            routes = mbta("/routes", **{"filter[stop]": sid})
            lines = [{"id": r["id"], "name": r["attributes"]["long_name"] if group != "bus" else r["attributes"]["short_name"],
                      "color": "#" + r["attributes"]["color"], "text": "#" + r["attributes"]["text_color"],
                      "dest": r["attributes"]["direction_destinations"]}
                     for r in routes if str(r["attributes"]["type"]) in rtype.split(",")]
            pos = (s["attributes"]["latitude"], s["attributes"]["longitude"])
            result[group].append({"id": sid, "name": s["attributes"]["name"], "lat": pos[0], "lng": pos[1],
                                  "lines": lines, "route": walk(origin, pos),
                                  "crow_m": round(haversine(origin, pos))})
    result["rapid"].sort(key=lambda s: s["route"]["minutes"])
    return result


def main():
    apts = json.loads((ROOT / "data" / "apartments.json").read_text())
    wanted = set(sys.argv[1:])
    for apt in apts:
        if wanted and apt["slug"] not in wanted:
            continue
        print(f"→ {apt['name']}", flush=True)
        try:
            build_one(apt)
        except Exception as e:  # noqa: BLE001 — keep going; one bad apartment shouldn't stop the batch
            print(f"  FAILED {apt['slug']}: {e!r}", flush=True)


def neighborhood_photos(apt, data):
    """Area photos whose titles name something real nearby (apartment, station, listed places)."""
    names = [apt["name"]] + [s["name"] for s in data["transit"]["rapid"]] + \
            [p["name"] for c in ("park", "grocery") for p in data[c]]
    words = sorted({w for n in names for w in key_words(n)})
    if not words:
        return []
    pics = photos(apt["lat"], apt["lng"], 1200, 6, words)
    taken = {ph["thumb"] for c in CATEGORIES for p in data[c] for ph in p["photos"]}
    return [ph for ph in pics if ph["thumb"] not in taken]


def build_one(apt):
    data = {"slug": apt["slug"], **{c: places(apt, c) for c in CATEGORIES}, "transit": transit(apt),
            "costco": costco(apt), "commute": commute(apt),}
    data["photos"] = neighborhood_photos(apt, data)
    (OUT / f"{apt['slug']}.json").write_text(json.dumps(data, ensure_ascii=False, indent=1))
    for c in CATEGORIES:
        print(f"  {c}: " + ", ".join(f"{p['name']} ({p['route']['minutes']}m{' 📷' if p['photo'] else ''})" for p in data[c]))
    print("  costco: " + ", ".join(f"{c['name']} ({c['route']['minutes']}m drive)" for c in data["costco"]))
    for code, c in data["commute"].items():
        legs = " → ".join(l["name"] if l["type"] == "ride" else f"walk {l['minutes']}" for l in c["transit"]["legs"])
        print(f"  {code}: drive {c['drive']['minutes']}m | transit {c['transit']['total']}m ({legs})")
    print(f"  photos: {len(data['photos'])}")
    for g, stops in data["transit"].items():
        print(f"  {g}: " + ", ".join(f"{s['name']} [{'/'.join(l['name'] for l in s['lines'])}]" for s in stops))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Build crime/safety data for the apartment page.

Outputs
  data/crime_areas.json  - town / neighborhood violent & property crime rates per 1,000 residents
  data/crime_near.json   - per-apartment incident counts near the building (last 12 full months)

Sources (all free, no auth except the FBI DEMO_KEY):
  - FBI Crime Data Explorer API (summarized agency data, NIBRS)      -> cities + MA + US
  - Analyze Boston "Crime Incident Reports" (CKAN datastore SQL)       -> Boston neighborhoods + near-apartment
  - Analyze Boston "Boston Population Estimates 2025 Neighborhood Level" (GeoJSON with polygons + population)
  - Cambridge open data Crime Reports (Socrata xuad-73uj) + Police Response Districts (vmh4-fu8c)
  - Somerville open data "Police Data: Crime Reports" (Socrata aghs-hqvg) + Census TIGERweb 2020 blocks

Run:  python3 -I scripts/crime.py      (stdlib only; raw responses cached in data/cache/crime_*.json)
Env:  FBI_API_KEY (default DEMO_KEY)
"""
import hashlib
import json
import math
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import date

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
CACHE = os.path.join(DATA, "cache")
AS_OF = "2026-10-26"
YEAR = 2025                                   # latest full calendar year
WIN_START, WIN_END = "2025-10-01", "2026-09-30"  # inclusive; last 12 full months
RADIUS_M = 400
FBI_KEY = os.environ.get("FBI_API_KEY", "DEMO_KEY")

BOS_SQL = "https://data.boston.gov/api/3/action/datastore_search_sql"
BOS_RES = "b973d8cb-eeb2-4e7e-99da-c92938efc9c0"  # Crime Incident Reports - 2023 to Present
BOS_CRIME_PAGE = "https://data.boston.gov/dataset/crime-incident-reports-august-2015-to-date-source-new-system"
BOS_POP_GEOJSON = ("https://gis.bostonplans.org/hosting/rest/services/Hosted/Data_2025_Neighborhood_AnalyzeB/"
                   "FeatureServer/0/query?where=1%3D1&outFields=name,population_b01001_001e&outSR=4326&f=geojson")
BOS_POP_PAGE = "https://data.boston.gov/dataset/2025-boston-population-estimates-neighborhood-level"
CAM_CRIME = "https://data.cambridgema.gov/resource/xuad-73uj.json"
CAM_CRIME_PAGE = "https://data.cambridgema.gov/Public-Safety/Crime-Reports/xuad-73uj"
CAM_DISTRICTS = "https://data.cambridgema.gov/resource/vmh4-fu8c.json"
SOM_CRIME = "https://data.somervillema.gov/resource/aghs-hqvg.json"
SOM_CRIME_PAGE = "https://data.somervillema.gov/Public-Safety/Police-Data-Crime-Reports/aghs-hqvg"
TIGER_BLOCKS = "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/tigerWMS_Census2020/MapServer/10/query"
CDE = "https://api.usa.gov/crime/fbi/cde/summarized/agency/{ori}/{off}?from=01-{y}&to=12-{y}&API_KEY={key}"
CDE_WEBAPP = "https://cde.ucr.cjis.gov/LATEST/summarized/agency/{ori}/{off}?from=01-{y}&to=12-{y}"
CDE_PAGE = "https://cde.ucr.cjis.gov/LATEST/webapp/#/pages/explorer/crime/crime-trend"

# FBI ORIs (from CDE /agency/byStateAbbr/MA)
AGENCIES = {
    "Cambridge": "MA0091100", "Somerville": "MA0093900", "Everett": "MA0091700",
    "Chelsea": "MA0130300", "Malden": "MA0092700", "Melrose": "MA0093100",
    "Revere": "MA0130400", "Medford": "MA0093000", "Boston (city)": "MA0130100",
}
# Boston neighborhoods (BPDA polygon name) -> BPD district that mostly covers it
BOS_HOODS = {
    "Jamaica Plain": "E13", "South Boston Waterfront": "C6", "Allston": "D14",
    "Brighton": "D14", "East Boston": "A7", "Charlestown": "A15",
}

# Apartment -> area (from address; verified against coordinates)
APT_AREA = {
    "the-pioneer": "Everett", "corey-10-luxury-apartments": "Melrose",
    "cambridge-park-apartments": "Cambridge", "the-laurent": "Cambridge",
    "hanover-alewife": "Cambridge", "walter-huntington-apartments": "Jamaica Plain",
    "bell-olmsted-park-apartments": "Jamaica Plain",
    "the-benjamin-seaport-residences-apartments": "South Boston Waterfront",
    "the-brynx": "Jamaica Plain", "avalon-at-assembly-row": "Somerville",
    "windsor-at-maxwell-s-green-apartments": "Somerville", "j-malden-center": "Malden",
    "maxwell": "Everett", "amaya": "Revere", "the-brookliner": "Brighton", "mason": "Everett",
    "v2-apartments": "Chelsea", "alder-at-allston-yards": "Allston", "the-eddy": "East Boston",
    "zinc": "Cambridge", "vero-apartments": "Chelsea",
}


def apt_area(slug):
    """Explicit map first; newer apartments (e.g. AI picks) fall back to data/areas.json."""
    if slug in APT_AREA:
        return APT_AREA[slug]
    areas = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "areas.json")))
    return areas["apartments"].get(slug)
NO_INCIDENT_DATA = {
    # What we could find instead of incident-level open data (checked 2026-10-26).
    "Everett": "Everett 未發布可下載的事件級犯罪開放資料；僅有 FBI CDE 全市月度統計（見區域數據）。",
    "Chelsea": "Chelsea 未發布可下載的事件級犯罪開放資料；僅有 FBI CDE 全市月度統計（見區域數據）。",
    "Malden": "Malden 未發布可下載的事件級犯罪開放資料；僅有 FBI CDE 全市月度統計（見區域數據）。",
    "Melrose": "Melrose 未發布可下載的事件級犯罪開放資料；僅有 FBI CDE 全市月度統計（見區域數據）。",
    "Revere": "Revere 未發布可下載的事件級犯罪開放資料；僅有 FBI CDE 全市月度統計（見區域數據）。",
    "Medford": "Medford 未發布可下載的事件級犯罪開放資料；僅有 FBI CDE 全市月度統計（見區域數據）。",
}


# ----------------------------------------------------------------------------- http + cache
def _cache_path(url, data=None):
    key = hashlib.sha1((url + "|" + (data or "")).encode()).hexdigest()
    return os.path.join(CACHE, "crime_%s.json" % key)


def fetch(url, data=None, pause=1.5, retries=4):
    path = _cache_path(url, data)
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)["body"]
    req = urllib.request.Request(url, data=data.encode() if data else None,
                                 headers={"User-Agent": "apartment-research/1.0"})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                body = json.loads(r.read().decode("utf-8"))
            break
        except Exception as e:  # noqa: BLE001 - retry any transient failure
            if attempt == retries - 1 or getattr(e, "code", None) in (403, 404, 429):
                raise
            print("  retry", attempt + 1, url[:90], e)
            time.sleep(5 * (attempt + 1))
    os.makedirs(CACHE, exist_ok=True)
    with open(path, "w") as f:
        json.dump({"url": url, "post": data, "fetched": time.strftime("%Y-%m-%dT%H:%M:%S"), "body": body}, f)
    time.sleep(pause)
    return body


def bos_sql(sql):
    body = fetch(BOS_SQL + "?" + urllib.parse.urlencode({"sql": sql}))
    if not body.get("success"):
        raise RuntimeError(body.get("error"))
    return body["result"]["records"]


def soda(url, params):
    return fetch(url + "?" + urllib.parse.urlencode(params))


# ----------------------------------------------------------------------------- geometry
def haversine(lat1, lon1, lat2, lon2):
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _rings(geom):
    if geom["type"] == "Polygon":
        return geom["coordinates"]
    if geom["type"] == "MultiPolygon":
        return [ring for poly in geom["coordinates"] for ring in poly]
    return []


def point_in(geom, lon, lat):
    """Even-odd ray casting over all rings (holes handled)."""
    inside = False
    for ring in _rings(geom):
        n = len(ring)
        j = n - 1
        for i in range(n):
            xi, yi = ring[i][0], ring[i][1]
            xj, yj = ring[j][0], ring[j][1]
            if (yi > lat) != (yj > lat) and lon < (xj - xi) * (lat - yi) / (yj - yi) + xi:
                inside = not inside
            j = i
    return inside


def bbox(lat, lon, m):
    dlat = m / 111320.0
    dlon = m / (111320.0 * math.cos(math.radians(lat)))
    return lat - dlat, lat + dlat, lon - dlon, lon + dlon


# ----------------------------------------------------------------------------- classification
# Near-apartment groups (user definition): violent = assault/robbery/homicide/sexual assault;
# property = burglary/larceny/auto theft/vandalism; other = other crimes.
# "excluded" = non-crime service calls & traffic (sick assist, accidents, lost property, warrants...).
def bos_group(code):
    c = int(code) if str(code).isdigit() else -1
    if c in (111, 301, 423, 801):
        return "violent"
    if c in (520, 530, 531, 540, 900, 1402, 1415) or 600 <= c <= 699 or c in (706, 724, 727):
        return "property"
    if (3000 <= c <= 3999 and c != 3170) or c in (1831, 1832, 2905, 2907, 2914, 2612, 2608, 2642):
        return "excluded"
    return "other"


def bos_fbi(code):
    """FBI-style Part I groups for area rates (rape is not in Boston's public data)."""
    c = int(code) if str(code).isdigit() else -1
    if c in (111, 301, 423):
        return "violent"
    if c in (520, 530, 531, 540) or 600 <= c <= 699 or c in (706, 724, 727):
        return "property"
    return None


CAM_VIOLENT = {"Simple Assault", "Aggravated Assault", "Street Robbery", "Commercial Robbery", "Homicide"}
CAM_PROPERTY = {"Shoplifting", "Larceny from Residence", "Larceny of Bicycle", "Mal. Dest. Property",
                "Larceny from MV", "Larceny from Building", "Housebreak", "Larceny (Misc)", "Auto Theft",
                "Commercial Break", "Larceny from Person", "Larceny of Plate", "Larceny of Services", "Arson"}
CAM_EXCLUDED = {"Hit and Run", "Accident", "Warrant Arrest", "Admin Error", "Missing Person", "Civil Dispute",
                "Medical", "Suspicious Package", "Elder Assistance/19A", "Encampment", "Community Engage",
                "Noise Complaint", "Hoarding", "Domestic Dispute", "Taxi Violation"}


def cam_group(crime):
    if crime in CAM_VIOLENT:
        return "violent"
    if crime in CAM_PROPERTY:
        return "property"
    if crime in CAM_EXCLUDED:
        return "excluded"
    return "other"


SOM_VIOLENT_TYPES = {"Robbery", "Sex Offenses", "Murder & Nonnegligent Manslaughter", "Homicide Offenses"}
SOM_PROPERTY_TYPES = {"Larceny/Theft Offenses", "Theft From Motor Vehicle", "Destruction/Vandalism Property",
                      "Burglary/Breaking And Entering", "Motor Vehicle Theft", "Arson"}


def som_group(r):
    off, typ = r.get("offense", ""), r.get("offensetype", "")
    if off in ("Simple Assault", "Aggravated Assault") or typ in SOM_VIOLENT_TYPES:
        return "violent"
    if typ in SOM_PROPERTY_TYPES:
        return "property"
    if typ == "Other Criminal MV Offenses":
        return "excluded"
    return "other"


def summarize(rows, group_fn, label_fn):
    g = Counter()
    top = Counter()
    for r in rows:
        k = group_fn(r)
        g[k] += 1
        if k != "excluded":
            top[label_fn(r)] += 1
    return {
        "violent": g["violent"], "property": g["property"], "other": g["other"],
        "excluded_noncrime_or_traffic": g["excluded"], "total_reports": sum(g.values()),
        "top": [[k, v] for k, v in top.most_common(6)],
    }


# ----------------------------------------------------------------------------- A) area level
def cde_year(ori, off):
    """api.usa.gov first; DEMO_KEY allows only ~10 calls/hour, so on HTTP 429 fall back to the
    key-less backend that the public CDE web app itself calls (same JSON)."""
    primary = CDE.format(ori=ori, off=off, y=YEAR, key=FBI_KEY)
    backup = CDE_WEBAPP.format(ori=ori, off=off, y=YEAR)
    if os.path.exists(_cache_path(backup)) and not os.path.exists(_cache_path(primary)):
        body = fetch(backup)
    else:
        try:
            body = fetch(primary, pause=3, retries=1)
        except urllib.error.HTTPError as e:
            if e.code != 429:
                raise
            body = fetch(backup, pause=3)
    o = body["offenses"]
    agency = [k for k in o["actuals"] if k.endswith(" Offenses")][0]
    months = o["actuals"][agency]
    pops = body["populations"]["population"][agency[: -len(" Offenses")]]
    return {
        "agency": agency[: -len(" Offenses")],
        "count": sum(v or 0 for v in months.values()), "months": len(months),
        "population": max(pops.values()),
        "ma_per_100k": sum(o["rates"]["Massachusetts Offenses"].values()),
        "us_per_100k": sum(o["rates"]["United States Offenses"].values()),
        "coverage": body.get("tooltips", {}).get("Percent of Population Coverage", {}),
    }


def build_areas(bos_hoods):
    cde = {}
    for name, ori in AGENCIES.items():
        cde[name] = {"V": cde_year(ori, "V"), "P": cde_year(ori, "P")}
    ref = cde["Cambridge"]
    us_v, us_p = ref["V"]["us_per_100k"] / 100, ref["P"]["us_per_100k"] / 100
    ma_v, ma_p = ref["V"]["ma_per_100k"] / 100, ref["P"]["ma_per_100k"] / 100
    cov_us = ref["V"]["coverage"].get("United States", {})
    cov_ma = ref["V"]["coverage"].get("Massachusetts", {})
    out = {
        "as_of": AS_OF,
        "national": {"violent_per_1k": round(us_v, 2), "property_per_1k": round(us_p, 2), "year": YEAR,
                     "source": "FBI Crime Data Explorer API, summarized NIBRS rates (sum of monthly "
                               "'United States Offenses' rates; based on reporting agencies, ~%.0f%% population "
                               "coverage — not the FBI's official national estimate)" % (min(cov_us.values()) if cov_us else float("nan")),
                     "source_url": CDE_PAGE},
        "massachusetts": {"violent_per_1k": round(ma_v, 2), "property_per_1k": round(ma_p, 2), "year": YEAR,
                          "source": "FBI Crime Data Explorer API, summarized NIBRS rates (sum of monthly "
                                    "'Massachusetts Offenses' rates; ~%.1f%% population coverage)" % (min(cov_ma.values()) if cov_ma else float("nan")),
                          "source_url": CDE_PAGE},
        "definitions": {
            "violent": "FBI violent crime: murder/non-negligent manslaughter, rape, robbery, aggravated assault (simple assault NOT included)",
            "property": "FBI property crime: burglary, larceny-theft, motor vehicle theft",
            "boston_neighborhoods": "Computed from Analyze Boston incident reports: offense codes 111/301/423 (violent) and "
                                    "520/530/531/540, 6xx larceny, 706/724/727 (property), deduplicated by incident+offense, "
                                    "geolocated incidents assigned to BPDA neighborhood polygons. Boston's public data omits "
                                    "sexual assaults, so Boston neighborhood violent rates exclude rape (undercount vs FBI).",
        },
        "areas": {},
    }

    def ratio_note(v, p):
        return "暴力犯罪約為全美平均的 %.1f 倍、財產犯罪約 %.1f 倍" % (v / us_v, p / us_p)

    city_notes = {
        "Cambridge": "Cambridge 全市（含 East Cambridge、Alewife）；2025 年 FBI NIBRS。",
        "Somerville": "Somerville 全市；2025 年 FBI NIBRS。",
        "Everett": "Everett 全市；2025 年 FBI NIBRS。",
        "Chelsea": "Chelsea 全市；2025 年 FBI NIBRS。",
        "Malden": "Malden 全市；2025 年 FBI NIBRS。",
        "Melrose": "Melrose 全市；2025 年 FBI NIBRS。",
        "Revere": "Revere 全市；2025 年 FBI NIBRS。",
        "Medford": "Medford 全市；2025 年 FBI NIBRS。",
    }
    for name, note in city_notes.items():
        v, p = cde[name]["V"], cde[name]["P"]
        pop = v["population"]
        vr, pr = 1000 * v["count"] / pop, 1000 * p["count"] / pop
        out["areas"][name] = {
            "violent_per_1k": round(vr, 2), "property_per_1k": round(pr, 2), "year": YEAR,
            "population": pop, "violent_count": v["count"], "property_count": p["count"],
            "months_reported": v["months"],
            "source_name": "FBI Crime Data Explorer (NIBRS), %s; population = FBI/CDE agency population" % v["agency"],
            "source_url": CDE_PAGE, "note_zh": note + ratio_note(vr, pr) + "。",
        }
    hood_notes = {
        "Jamaica Plain": "按 BPDA 社區邊界統計（主要屬 BPD E-13 區）。",
        "South Boston Waterfront": "Seaport；按 BPDA 社區邊界統計（屬 BPD C-6 區）；居民少、上班與遊客人口多，比率需謹慎解讀。",
        "Allston": "按 BPDA 社區邊界統計（BPD D-14 區與 Brighton 合用）。",
        "Brighton": "按 BPDA 社區邊界統計（BPD D-14 區與 Allston 合用）。",
        "East Boston": "按 BPDA 社區邊界統計（BPD A-7 區，含機場範圍）。",
        "Charlestown": "按 BPDA 社區邊界統計（BPD A-15 區，含 Navy Yard）。",
    }
    bv, bp = cde["Boston (city)"]["V"], cde["Boston (city)"]["P"]
    v_scale = bv["count"] / bos_hoods["_city"]["violent"]   # FBI citywide / open-data citywide
    p_scale = bp["count"] / bos_hoods["_city"]["property"]
    for name in BOS_HOODS:
        h = bos_hoods[name]
        vr, pr = 1000 * h["violent"] / h["population"], 1000 * h["property"] / h["population"]
        out["areas"][name] = {
            "violent_per_1k": round(vr, 2), "property_per_1k": round(pr, 2), "year": YEAR,
            "population": h["population"], "violent_count": h["violent"], "property_count": h["property"],
            "bpd_district": h["district"], "district_counts_incl_ungeocoded": h["district_counts"],
            "source_name": "Analyze Boston – Crime Incident Reports (BPD), %d; population = City of Boston Planning Dept. "
                           "Research Division neighborhood estimate for 2025-01-01" % YEAR,
            "violent_per_1k_scaled_to_fbi": round(vr * v_scale, 2),
            "property_per_1k_scaled_to_fbi": round(pr * p_scale, 2),
            "scaled_note": "ESTIMATE: open-data rate x (FBI Boston citywide %d count / open-data citywide count); "
                           "violent factor %.2f, property factor %.2f. Use only for rough comparison with FBI-based city rates."
                           % (YEAR, v_scale, p_scale),
            "source_url": BOS_CRIME_PAGE, "population_source_url": BOS_POP_PAGE,
            "note_zh": hood_notes[name] + ("Boston 公開資料的暴力犯罪數只有 FBI 全市統計的約 %d%%（不含性侵等），"
                                           "按比例估算約 %.1f／千人（全美 %.1f）。" % (round(100 / v_scale), vr * v_scale, us_v)),
        }
    # Boston citywide: FBI figure vs our open-data mapping, as a sanity check
    out["boston_city_reference"] = {
        "fbi_violent_per_1k": round(1000 * bv["count"] / bv["population"], 2),
        "fbi_property_per_1k": round(1000 * bp["count"] / bp["population"], 2),
        "fbi_population": bv["population"], "fbi_violent_count": bv["count"], "fbi_property_count": bp["count"],
        "open_data_violent_count": bos_hoods["_city"]["violent"], "open_data_property_count": bos_hoods["_city"]["property"],
        "year": YEAR, "note": "Sanity check of the open-data offense-code mapping used for Boston neighborhoods "
                              "(open-data counts include ungeocoded rows; exclude rape).",
    }
    return out


def boston_neighborhoods():
    geo = fetch(BOS_POP_GEOJSON)
    polys = {f["properties"]["name"]: f for f in geo["features"]}
    codes = "('111','301','423','520','530','531','540','706','724','727'," + \
            ",".join("'%d'" % c for c in range(600, 700)) + ")"
    rows, off = [], 0
    while True:
        page = bos_sql(
            'SELECT "_id","INCIDENT_NUMBER","OFFENSE_CODE","DISTRICT","Lat","Long" FROM "%s" '
            "WHERE \"OCCURRED_ON_DATE\" >= '%d-01-01' AND \"OCCURRED_ON_DATE\" < '%d-01-01' "
            'AND "OFFENSE_CODE" IN %s ORDER BY "_id" LIMIT 10000 OFFSET %d' % (BOS_RES, YEAR, YEAR + 1, codes, off))
        rows += page
        if len(page) < 10000:
            break
        off += 10000
    seen, uniq = set(), []
    for r in rows:
        k = (r["INCIDENT_NUMBER"], r["OFFENSE_CODE"])
        if k not in seen:
            seen.add(k)
            uniq.append(r)
    res = {}
    dist = {}
    for r in uniq:
        g = bos_fbi(r["OFFENSE_CODE"])
        d = dist.setdefault(r["DISTRICT"], Counter())
        d[g] += 1
        d["geocoded_" + g] += 1 if r["Lat"] else 0
    for name, district in BOS_HOODS.items():
        f = polys[name]
        c = Counter()
        for r in uniq:
            try:
                lat, lon = float(r["Lat"]), float(r["Long"])
            except (TypeError, ValueError):
                continue
            if point_in(f["geometry"], lon, lat):
                c[bos_fbi(r["OFFENSE_CODE"])] += 1
        dc = dist.get(district, Counter())
        res[name] = {"violent": c["violent"], "property": c["property"],
                     "population": int(f["properties"]["population_b01001_001e"]), "district": district,
                     "district_counts_incl_ungeocoded": None,
                     "district_counts": {"violent": dc["violent"], "property": dc["property"],
                                         "geocoded_share": round((dc["geocoded_violent"] + dc["geocoded_property"]) /
                                                                 max(1, dc["violent"] + dc["property"]), 3)}}
    tot = Counter(bos_fbi(r["OFFENSE_CODE"]) for r in uniq)
    res["_city"] = {"violent": tot["violent"], "property": tot["property"]}
    return res, polys


# ----------------------------------------------------------------------------- B) near apartment
def near_boston(apt, polys):
    lat0, lon0 = apt["lat"], apt["lng"]
    la1, la2, lo1, lo2 = bbox(lat0, lon0, RADIUS_M * 1.1)
    rows = bos_sql(
        'SELECT "INCIDENT_NUMBER","OFFENSE_CODE","OFFENSE_DESCRIPTION","Lat","Long" FROM "%s" '
        "WHERE \"OCCURRED_ON_DATE\" >= '%s' AND \"OCCURRED_ON_DATE\" < '2026-10-01' "
        "AND \"Lat\" IS NOT NULL AND \"Lat\" <> '' AND CAST(\"Lat\" AS float) BETWEEN %f AND %f "
        "AND CAST(\"Long\" AS float) BETWEEN %f AND %f LIMIT 30000" % (BOS_RES, WIN_START, la1, la2, lo1, lo2))
    seen, inside = set(), []
    for r in rows:
        k = (r["INCIDENT_NUMBER"], r["OFFENSE_CODE"])
        if k in seen:
            continue
        seen.add(k)
        if haversine(lat0, lon0, float(r["Lat"]), float(r["Long"])) <= RADIUS_M:
            inside.append(r)
    s = summarize(inside, lambda r: bos_group(r["OFFENSE_CODE"]), lambda r: r["OFFENSE_DESCRIPTION"])
    # share of the 400 m circle that falls inside Boston neighborhood polygons (rest = other town or water)
    pts = in_bos = 0
    for i in range(-10, 11):
        for j in range(-10, 11):
            dy, dx = i * RADIUS_M / 10, j * RADIUS_M / 10
            if dx * dx + dy * dy > RADIUS_M ** 2:
                continue
            pts += 1
            la = lat0 + dy / 111320.0
            lo = lon0 + dx / (111320.0 * math.cos(math.radians(lat0)))
            in_bos += any(point_in(f["geometry"], lo, la) for f in polys.values() if f.get("geometry"))
    share = round(in_bos / pts, 2)
    method = ("Boston 警方事件資料（有經緯度）：統計大樓 400 公尺半徑內、%s 至 %s 發生的事件；"
              "同一案件同一罪名只算一次；不含性侵（公開資料未提供）及非犯罪類服務紀錄。" % (WIN_START, WIN_END))
    hood = next((n for n, f in polys.items() if f.get("geometry") and point_in(f["geometry"], lon0, lat0)), None)
    if hood != apt_area(apt["slug"]):
        method += "（依 BPDA 社區邊界，此地址屬 %s。）" % hood
    if share < 0.9:
        method += "半徑圈約 %d%% 位於 Boston 社區邊界內，其餘為鄰鎮或水域（鄰鎮事件未計入）。" % round(share * 100)
    s.update({"method_zh": method, "geography": "radius", "bpda_neighborhood": hood, "circle_share_in_boston": share,
              "source_url": BOS_CRIME_PAGE})
    return s


def cambridge_data():
    rows = soda(CAM_CRIME, {"$select": "file_number,date_of_report,crime,reporting_area,neighborhood,"
                                       "reporting_area_lat,reporting_area_lon",
                            "$where": "date_of_report>='%sT00:00:00' AND date_of_report<'2026-10-01T00:00:00'" % WIN_START,
                            "$limit": "50000"})
    districts = soda(CAM_DISTRICTS, {"$limit": "5000"})
    return rows, districts


def near_cambridge(apt, rows, districts):
    lat0, lon0 = apt["lat"], apt["lng"]
    area = next((d["cadra"] for d in districts if point_in(d["the_geom"], lon0, lat0)), None)
    in_area = [r for r in rows if r.get("reporting_area") == area]
    hood = Counter(r.get("neighborhood") for r in in_area).most_common(1)
    s = summarize(in_area, lambda r: cam_group(r.get("crime")), lambda r: r.get("crime"))
    # supplementary: reporting areas whose centroid is within 400 m
    cents = {}
    for r in rows:
        if r.get("reporting_area") and r.get("reporting_area_lat"):
            cents[r["reporting_area"]] = (float(r["reporting_area_lat"]), float(r["reporting_area_lon"]))
    near_areas = sorted({a for a, (la, lo) in cents.items() if haversine(lat0, lon0, la, lo) <= RADIUS_M} | {area})
    alt = summarize([r for r in rows if r.get("reporting_area") in near_areas],
                    lambda r: cam_group(r.get("crime")), lambda r: r.get("crime"))
    s.update({
        "geography": "reporting_area", "reporting_area": area, "neighborhood": hood[0][0] if hood else None,
        "method_zh": ("Cambridge 資料只有警方報案區（reporting area）與街段，沒有精確座標；此處統計大樓所在報案區 %s"
                      "（%s 社區）%s 至 %s 的報案（依報案日期）。資料集中沒有性侵類別。" %
                      (area, hood[0][0] if hood else "?", WIN_START, WIN_END)),
        "alt_own_area_plus_areas_centroid_within_400m": {"reporting_areas": near_areas, "violent": alt["violent"],
                                           "property": alt["property"], "other": alt["other"],
                                           "total_reports": alt["total_reports"]},
        "source_url": CAM_CRIME_PAGE,
    })
    return s


def somerville_data():
    rows = soda(SOM_CRIME, {"$select": "incnum,day_and_month,year,offense,offensetype,category,blockcode",
                            "$where": "year in('2025','2026')", "$limit": "50000"})
    out, last = [], None
    for r in rows:
        try:
            m, d = (int(x) for x in r["day_and_month"].split("/"))
            dt = date(int(r["year"]), m, d)
        except (KeyError, ValueError):
            continue
        last = max(last, dt) if last else dt
        if date.fromisoformat(WIN_START) <= dt <= date.fromisoformat(WIN_END):
            r["blockcode"] = (r.get("blockcode") or "").strip()
            out.append(r)
    return out, last


def near_somerville(apt, rows):
    lat0, lon0 = apt["lat"], apt["lng"]
    q = {"geometry": "%f,%f" % (lon0, lat0), "geometryType": "esriGeometryPoint", "inSR": "4326",
         "spatialRel": "esriSpatialRelIntersects", "distance": str(RADIUS_M * 2), "units": "esriSRUnit_Meter",
         "outFields": "GEOID,INTPTLAT,INTPTLON", "returnGeometry": "false", "f": "json"}
    body = fetch(TIGER_BLOCKS, urllib.parse.urlencode(q))
    q2 = dict(q, distance="0")
    home = fetch(TIGER_BLOCKS, urllib.parse.urlencode(q2))
    blocks = {f["attributes"]["GEOID"] for f in body["features"]
              if haversine(lat0, lon0, float(f["attributes"]["INTPTLAT"]), float(f["attributes"]["INTPTLON"])) <= RADIUS_M}
    blocks |= {f["attributes"]["GEOID"] for f in home["features"]}
    sel = [r for r in rows if r["blockcode"] in blocks]
    s = summarize(sel, som_group, lambda r: r.get("offense"))
    s.update({
        "geography": "census_blocks", "census_blocks_2020": len(blocks),
        "method_zh": ("Somerville 資料只有 2020 人口普查街區代碼（沒有座標）；統計街區中心點在大樓 400 公尺內"
                      "（含大樓所在街區）的 %d 個街區、%s 至 %s 的犯罪報告；只含 Somerville 市內，"
                      "跨市界部分未計入。" % (len(blocks), WIN_START, WIN_END)),
        "source_url": SOM_CRIME_PAGE,
    })
    return s


def main():
    with open(os.path.join(DATA, "apartments.json")) as f:
        apts = json.load(f)
    apts = apts if isinstance(apts, list) else apts.get("apartments", list(apts.values()))

    print("Boston neighborhoods ...")
    hoods, polys = boston_neighborhoods()
    for h in hoods.values():
        h.pop("district_counts_incl_ungeocoded", None)
    print("FBI CDE ...")
    areas = build_areas(hoods)
    with open(os.path.join(DATA, "crime_areas.json"), "w") as f:
        json.dump(areas, f, ensure_ascii=False, indent=1)

    print("Near-apartment ...")
    cam_rows, cam_districts = cambridge_data()
    som_rows, som_last = somerville_data()
    near = {"as_of": AS_OF, "radius_m": RADIUS_M, "window": "%s..%s" % (WIN_START, WIN_END),
            "groups": {"violent": "assault (simple+aggravated), robbery, homicide, sexual assault (where published)",
                       "property": "burglary/B&E, larceny/theft (incl. shoplifting, from MV, bicycle), auto theft, vandalism, arson",
                       "other": "other criminal offenses (fraud, threats, harassment, drugs, trespass, weapons, OUI...)",
                       "excluded_noncrime_or_traffic": "non-crime service records & traffic (sick assist, accidents, "
                                                       "hit-and-run, lost property, warrants, MV-law violations) — not in violent/property/other"},
            "comparability_note_zh": "三市地理方法不同：Boston 為 400 公尺半徑；Cambridge 為大樓所在報案區（面積通常小於半徑圈）；"
                                     "Somerville 為中心點在 400 公尺內的普查街區。跨城市比較數字時請保留誤差空間。"
                                     "Everett、Chelsea、Malden、Melrose、Revere 無事件級公開資料。",
            "data_through": {"boston": None, "cambridge": max((r["date_of_report"][:10] for r in cam_rows), default=None),
                             "somerville": som_last.isoformat() if som_last else None},
            "apartments": {}}
    for a in apts:
        slug = a["slug"]
        area = apt_area(slug)
        print(" ", slug, area)
        if area in BOS_HOODS:
            s = near_boston(a, polys)
        elif area == "Cambridge":
            s = near_cambridge(a, cam_rows, cam_districts)
        elif area == "Somerville":
            s = near_somerville(a, som_rows)
        else:
            s = {"violent": None, "property": None, "other": None, "top": [],
                 "method_zh": NO_INCIDENT_DATA[area], "source_url": CDE_PAGE}
        s = dict({"area": area, "available": s.get("violent") is not None}, **s)
        near["apartments"][slug] = s
    last_bos = bos_sql('SELECT max("OCCURRED_ON_DATE") AS m FROM "%s"' % BOS_RES)[0]["m"]
    near["data_through"]["boston"] = (last_bos or "")[:10]
    with open(os.path.join(DATA, "crime_near.json"), "w") as f:
        json.dump(near, f, ensure_ascii=False, indent=1)
    print("done")


if __name__ == "__main__":
    main()

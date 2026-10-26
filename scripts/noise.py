"""Transportation noise per apartment from the US DOT BTS National Transportation Noise Map (2020).

Usage: python3 -I scripts/noise.py        (stdlib only; writes data/noise.json)

Values: BTS 2020 modeled 24-hour LAeq (dBA) for aviation, rail, road and all three combined,
30 m grid. Read from Esri Living Atlas' scientific (float32) republication of the BTS rasters,
"USA Transportation Noise 2020" (one multidimensional image service, one slice per BTS layer),
because BTS's own services are rendered PNG tiles with coarse colour classes only. The tiles are
LERC2 (lossless float) blobs, decoded below. In that layer Esri set "below 45 dBA" (NoData in the
BTS model) to exactly 45.0; we report those as null and list them in `below_threshold`.

Also: nearest listed highway and nearest surface rail line (OpenStreetMap via Overpass, straight
line distance to the way geometry), and a Leaflet tile URL for BTS's own 2020 combined layer.
Raw responses are cached in data/cache/ (prefix noise_), so re-runs are offline.
"""
import hashlib
import json
import math
import struct
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "data" / "cache"
OUT = ROOT / "data" / "noise.json"
UA = "boston-apartments-personal/1.0 (noise lookup)"
AS_OF = "2026-10-26"

ESRI_ITEM = "https://www.arcgis.com/home/item.html?id=439250230a414e46aa2846c7e2ae2048"
ESRI_SVC = ("https://tiledimageservices.arcgis.com/P3ePLMYs2RVChkJx/arcgis/rest/services/"
            "USA_Transportation_Noise_2020/ImageServer")
BTS_PAGE = "https://www.bts.gov/geospatial/national-transportation-noise-map"
BTS_TILES = "https://tiles.arcgis.com/tiles/xOi1kZaI0eWDREZv/arcgis/rest/services"
OVERLAY_SVC = BTS_TILES + "/NTAD_Noise_2020_CONUS_aviation_rail_road/MapServer"
OVERPASS = ["https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter",
            "https://overpass.private.coffee/api/interpreter"]

LAYERS = {"aviation": "Aviation", "rail": "Rail", "combined": "Rail Road and Aviation", "road": "Road"}
LEVEL = 9            # 30 m level of the service's Albers tiling scheme
BACKDROP = 45.0      # Esri's fill value for "below the BTS modeled threshold"
RADIUS_M = 50        # neighbourhood for max_50m_db
HIGHWAYS = {("US:I", "93"), ("US:US", "1"), ("US:MA", "2"), ("US:I", "90"), ("US:MA", "16"), ("US:MA", "1A")}
HIGHWAY_LABEL = {"US:I": "I-", "US:US": "US ", "US:MA": "Route "}
COMMUTER_LINES = {  # OSM names of MBTA commuter-rail routes -> line names riders know
    "Eastern Route": "Newburyport/Rockport Line", "Eastern Route Main Line": "Newburyport/Rockport Line",
    "Western Route": "Haverhill Line", "New Hampshire Route": "Lowell Line",
    "New Hampshire Route Main Line": "Lowell Line", "Fitchburg Route": "Fitchburg Line",
    "Boston Subdivision": "Framingham/Worcester Line", "Worcester Main Line": "Framingham/Worcester Line",
}
SCALE_ZH = [[45, "很安靜"], [55, "一般住宅區"], [65, "吵（相當於繁忙街道）"], [75, "很吵"]]
MODE_ZH = {"road": "道路", "aviation": "飛機", "rail": "鐵路"}


# ---------------------------------------------------------------- fetching with cache

def fetch(url, data=None, kind="json", tries=4, delay=1.0, timeout=120, mirrors=None):
    key = hashlib.sha1((url + (data or "")).encode()).hexdigest()
    path = CACHE / f"noise_{key}.{'json' if kind == 'json' else 'bin'}"
    if path.exists():
        raw = path.read_bytes()
        return json.loads(raw) if kind == "json" else raw
    urls = mirrors or [url]
    for attempt in range(tries):
        u = urls[attempt % len(urls)]
        try:
            req = urllib.request.Request(u, data=data.encode() if data else None, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
            payload = json.loads(raw) if kind == "json" else raw
            if kind == "json" and isinstance(payload, dict) and "error" in payload:
                raise RuntimeError(payload["error"])
            CACHE.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
            time.sleep(delay)  # be gentle with free public services
            return payload
        except Exception as e:  # noqa: BLE001
            print(f"  retry {attempt + 1}/{tries} {u[:80]}: {e}", file=sys.stderr)
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"failed: {url}")


# ---------------------------------------------------------------- projection (ESRI:102039, NAD83 Albers)

_A, _F = 6378137.0, 1 / 298.257222101
_E2 = 2 * _F - _F * _F
_E = math.sqrt(_E2)


def _q(phi):
    s = math.sin(phi)
    return (1 - _E2) * (s / (1 - _E2 * s * s) - math.log((1 - _E * s) / (1 + _E * s)) / (2 * _E))


def _m(phi):
    s = math.sin(phi)
    return math.cos(phi) / math.sqrt(1 - _E2 * s * s)


_P0, _P1, _P2, _L0 = map(math.radians, (23.0, 29.5, 45.5, -96.0))
_N = (_m(_P1) ** 2 - _m(_P2) ** 2) / (_q(_P2) - _q(_P1))
_C = _m(_P1) ** 2 + _N * _q(_P1)
_R0 = _A * math.sqrt(_C - _N * _q(_P0)) / _N


def albers(lat, lng):
    """WGS84/NAD83 lat/lng → USA Contiguous Albers (USGS) metres. Datum shift (~1 m) ignored."""
    r = _A * math.sqrt(_C - _N * _q(math.radians(lat))) / _N
    t = _N * (math.radians(lng) - _L0)
    return r * math.sin(t), _R0 - r * math.cos(t)


# ---------------------------------------------------------------- LERC2 v6 decoder (float32, lossless "FPL" mode)
# Port of the decode path in github.com/Esri/lerc (Lerc2.cpp, fpl_*.cpp, Huffman.cpp, BitStuffer2.cpp).

def _unstuff(b, p, n, nbits):
    nbytes = (n * nbits + 7) // 8
    v = int.from_bytes(b[p:p + nbytes], "little")
    mask = (1 << nbits) - 1
    return [(v >> (i * nbits)) & mask for i in range(n)], p + nbytes


def _bitstuffer2(b, p):
    head = b[p]; p += 1
    nb = 4 if head >> 6 == 0 else 3 - (head >> 6)
    use_lut, nbits = bool(head & 32), head & 31
    n = int.from_bytes(b[p:p + nb], "little"); p += nb
    if not use_lut:
        return ([0] * n, p) if nbits == 0 else _unstuff(b, p, n, nbits)
    nlut = b[p] - 1; p += 1
    lut, p = _unstuff(b, p, nlut, nbits)
    idx, p = _unstuff(b, p, n, nlut.bit_length())
    lut = [0] + lut
    return [lut[i] for i in idx], p


def _huffman(b, p, count):
    _ver, size, i0, i1 = struct.unpack_from("<4i", b, p); p += 16
    lens, p = _bitstuffer2(b, p)
    tail = b[p:] + b"\0" * (-(len(b) - p) % 4)
    bits = "".join(format(w, "032b") for w in struct.unpack(f"<{len(tail) // 4}I", tail))
    codes, pos = {}, 0
    for i in range(i0, i1):
        if lens[i - i0]:
            codes[bits[pos:pos + lens[i - i0]]] = i if i < size else i - size
            pos += lens[i - i0]
    pos = (pos + 31) // 32 * 32
    lengths = sorted({len(c) for c in codes})
    out = bytearray(count)
    for m in range(count):
        for L in lengths:
            k = codes.get(bits[pos:pos + L])
            if k is not None:
                out[m] = k; pos += L
                break
        else:
            raise ValueError("corrupt Huffman stream")
    return out


def _byte_plane(buf, count):
    flag = buf[0]
    if flag == 1:                       # whole plane is one value
        return bytearray([buf[1]]) * count
    if flag == 2:                       # stored as is
        return bytearray(buf[1:1 + count])
    if flag == 3:                       # PackBits
        out, i = bytearray(), 1
        while i < len(buf):
            h = buf[i]; i += 1
            if h <= 127:
                out += buf[i:i + h + 1]; i += h + 1
            else:
                out += bytes([buf[i]]) * (h - 126); i += 1
        return out
    return _huffman(buf, 1, count)


def _add(a, b):  # ADD32_BIT_FLT: 23-bit mantissa field + 9-bit (exp, sign) field
    return ((a + b) & 0x7FFFFF) | ((((a >> 23) + (b >> 23)) & 0x1FF) << 23)


def _fpl(b, p, w, h):
    pred = b[p]; p += 1
    n = w * h
    planes = {}
    for _ in range(4):
        idx, level = b[p], b[p + 1]
        size = struct.unpack_from("<I", b, p + 2)[0]; p += 6
        d = _byte_plane(b[p:p + size], n); p += size
        for lv in range(level, 0, -1):
            for i in range(lv, n):
                d[i] = (d[i] + d[i - 1]) & 0xFF
        planes[idx] = d
    u = [planes[0][i] | planes[1][i] << 8 | planes[2][i] << 16 | planes[3][i] << 24 for i in range(n)]
    if pred == 2:                       # rows+cols predictor: undo column deltas first
        for r in range(1, h):
            o = r * w
            for c in range(w):
                u[o + c] = _add(u[o + c], u[o - w + c])
    if pred in (1, 2):
        for r in range(h):
            o = r * w
            for c in range(1, w):
                u[o + c] = _add(u[o + c], u[o + c - 1])
    u = [(a & 0x7FFFFF) | ((a >> 24) & 0xFF) << 23 | ((a >> 23) & 1) << 31 for a in u]
    return list(struct.unpack(f"<{n}f", struct.pack(f"<{n}I", *u)))


def _tiles(b, p, nr, nc, mb, valid, max_err, zmax):
    """Lerc2 micro-block ("tiling") mode, single band float32."""
    out = [0.0] * (nr * nc)
    for i0 in range(0, nr, mb):
        for j0 in range(0, nc, mb):
            cells = [i * nc + j for i in range(i0, min(i0 + mb, nr)) for j in range(j0, min(j0 + mb, nc))]
            cells = [k for k in cells if valid is None or valid[k]]
            flag = b[p]; p += 1
            if ((flag >> 2) & 14) != ((j0 >> 3) & 14) or flag & 4:
                raise ValueError("Lerc2 tile integrity check failed")
            bits67, flag = flag >> 6, flag & 3
            if flag == 2:                    # constant 0
                vs = [0.0] * len(cells)
            elif flag == 0:                  # raw float32
                vs = struct.unpack_from(f"<{len(cells)}f", b, p); p += 4 * len(cells)
            else:
                fmt = {0: "<f", 1: "<h", 2: "<B"}[bits67]
                offset = struct.unpack_from(fmt, b, p)[0]; p += struct.calcsize(fmt)
                if flag == 3:                # constant offset
                    vs = [offset] * len(cells)
                else:                        # bit-stuffed quantised ints
                    q, p = _bitstuffer2(b, p)
                    vs = [min(offset + v * 2 * max_err, zmax) for v in q]
            for k, v in zip(cells, vs):
                out[k] = v
    return out


def lerc_decode(b):
    """Return (rows, cols, values) with None for masked pixels."""
    if b[:6] != b"Lerc2 ":
        raise ValueError("not a Lerc2 blob")
    ver, _chk, nr, nc, nd, nvalid, mb, _blob, dt = struct.unpack_from("<iIiiiiiii", b, 6)
    if ver != 6 or nd != 1 or dt != 6:
        raise ValueError(f"unsupported Lerc2 v{ver} depth {nd} type {dt}")
    p = 6 + 36 + 8
    max_err, zmin, zmax = struct.unpack_from("<3d", b, p); p += 40
    nmask = struct.unpack_from("<i", b, p)[0]; p += 4
    valid = None
    if nmask > 0:                       # RLE-compressed validity bitmask
        raw, q = bytearray(), p
        while True:
            cnt = struct.unpack_from("<h", b, q)[0]; q += 2
            if cnt == -32768:
                break
            if cnt > 0:
                raw += b[q:q + cnt]; q += cnt
            else:
                raw += bytes([b[q]]) * -cnt; q += 1
        valid = [(raw[k >> 3] >> (7 - (k & 7))) & 1 for k in range(nr * nc)]
        p += nmask
    if nvalid == 0:
        return nr, nc, [None] * (nr * nc)
    if zmin == zmax:
        vals = [zmin] * (nr * nc)
    else:
        p += 8                          # per-band min/max
        one_sweep = b[p]; p += 1
        if one_sweep:
            packed = struct.unpack_from(f"<{nvalid}f", b, p)
            if valid is None:
                return nr, nc, list(packed)
            it = iter(packed)
            return nr, nc, [next(it) if v else None for v in valid]
        mode = b[p] if max_err == 0 else 0  # mode byte only present for lossless float
        p += max_err == 0
        if mode == 3:
            vals = _fpl(b, p, nc, nr)
        elif mode == 0:
            vals = _tiles(b, p, nr, nc, mb, valid, max_err, zmax)
        else:
            raise ValueError(f"unsupported Lerc2 encoding mode {mode}")
    if valid is not None:
        vals = [v if ok else None for v, ok in zip(vals, valid)]
    return nr, nc, vals


# ---------------------------------------------------------------- BTS noise sampling

class NoiseRaster:
    def __init__(self):
        info = fetch(ESRI_SVC + "?f=json")
        ti = info["tileInfo"]
        lod = next(l for l in ti["lods"] if l["level"] == LEVEL)
        self.res, self.ox, self.oy, self.size = lod["resolution"], ti["origin"]["x"], ti["origin"]["y"], ti["rows"]
        assert info["spatialReference"]["wkid"] == 102039, info["spatialReference"]
        slices = fetch(ESRI_SVC + "/slices?f=json")["slices"]
        by_var = {s["multidimensionalDefinition"][0]["variableName"]: s["sliceId"] for s in slices}
        self.slice = {k: by_var[v] for k, v in LAYERS.items()}
        self.tiles = {}

    def _tile(self, layer, row, col):
        key = (layer, row, col)
        if key not in self.tiles:
            url = f"{ESRI_SVC}/tile/{LEVEL}/{row}/{col}?sliceId={self.slice[layer]}"
            self.tiles[key] = lerc_decode(fetch(url, kind="bin"))[2]
        return self.tiles[key]

    def pixel(self, layer, gx, gy):
        t = self._tile(layer, gy // self.size, gx // self.size)
        return t[(gy % self.size) * self.size + gx % self.size]

    def grid(self, lat, lng):
        x, y = albers(lat, lng)
        return (x - self.ox) / self.res, (self.oy - y) / self.res

    def at(self, layer, lat, lng):
        fx, fy = self.grid(lat, lng)
        return self.pixel(layer, int(fx), int(fy))

    def max_within(self, layer, lat, lng, radius):
        fx, fy = self.grid(lat, lng)
        r = radius / self.res
        best = None
        for gy in range(int(fy - r) - 1, int(fy + r) + 2):
            for gx in range(int(fx - r) - 1, int(fx + r) + 2):
                if math.hypot(gx + 0.5 - fx, gy + 0.5 - fy) <= r:  # pixel centre within radius
                    v = self.pixel(layer, gx, gy)
                    if v is not None and (best is None or v > best):
                        best = v
        return best


def db(v):
    """None when the BTS model has no value (Esri's 45.0 backdrop) → quieter than ~45 dBA."""
    return None if v is None or v <= BACKDROP else round(v, 1)


# ---------------------------------------------------------------- OSM distances

def osm_lines(apts):
    """Highway ways from the road-route relations (OSM puts US 1 / Route 2 refs on relations, not
    always on the ways) plus surface rail/light rail/subway main tracks, around all apartments."""
    lats, lngs = [a["lat"] for a in apts], [a["lng"] for a in apts]
    bbox = f"{min(lats) - 0.03:.3f},{min(lngs) - 0.04:.3f},{max(lats) + 0.03:.3f},{max(lngs) + 0.04:.3f}"
    refs = "|".join(sorted({r for _, r in HIGHWAYS}))
    q = (f'[out:json][timeout:180];'
         f'rel["type"="route"]["route"="road"]["network"~"^US:(I|US|MA)$"]["ref"~"^({refs})$"]({bbox})->.r;'
         f'.r out body;'
         f'way(r.r)["highway"]["highway"!~"_link$"]["tunnel"!~"."]({bbox});out tags geom;'
         f'way["railway"~"^(rail|light_rail|subway)$"]["tunnel"!~"."]["service"!~"."]({bbox});out tags geom;')
    res = fetch(OVERPASS[0], data="data=" + urllib.parse.quote(q), mirrors=OVERPASS, timeout=240)
    route_of = {}  # way id -> highway label
    for e in res["elements"]:
        t = e.get("tags", {})
        if e["type"] == "relation" and (t.get("network"), t.get("ref")) in HIGHWAYS:
            label = HIGHWAY_LABEL[t["network"]] + t["ref"]
            for m in e["members"]:
                if m["type"] == "way":
                    route_of.setdefault(m["ref"], label)
    hw, rail = [], []
    for e in res["elements"]:
        t, pts = e.get("tags", {}), [(g["lat"], g["lon"]) for g in e.get("geometry", [])]
        if e["type"] != "way" or len(pts) < 2:
            continue
        if "highway" in t and e["id"] in route_of:
            hw.append((route_of[e["id"]] + (f"（{t['name']}）" if t.get("name") else ""), pts))
        elif "railway" in t and t.get("usage") in (None, "main", "branch") and "Test Track" not in t.get("name", ""):
            rail.append((rail_label(t), pts))
    return hw, rail


def rail_label(t):
    name = t.get("name", "")
    if not name or name.startswith("Track ") or name.endswith(" Int") or name.endswith(" Iron"):
        op = {"Massachusetts Bay Transportation Authority": "MBTA", "MBTA": "MBTA", "Amtrak": "Amtrak",
              "CSX Transportation": "CSX"}.get(t.get("operator"), "")
        return (op + " rail line").strip()
    return name + (f" ({COMMUTER_LINES[name]})" if name in COMMUTER_LINES else "")


def seg_dist(p, a, b):
    """Metres from p to segment ab (local equirectangular, fine at <10 km)."""
    k = math.cos(math.radians(p[0]))
    ax, ay = (a[1] - p[1]) * k * 111320, (a[0] - p[0]) * 110574
    bx, by = (b[1] - p[1]) * k * 111320, (b[0] - p[0]) * 110574
    dx, dy = bx - ax, by - ay
    L = dx * dx + dy * dy
    t = 0 if L == 0 else max(0, min(1, -(ax * dx + ay * dy) / L))
    return math.hypot(ax + t * dx, ay + t * dy)


def nearest(p, lines):
    best = None
    for name, pts in lines:
        d = min(seg_dist(p, pts[i], pts[i + 1]) for i in range(len(pts) - 1))
        if best is None or d < best[1]:
            best = (name, d)
    return {"name": best[0], "m": int(round(best[1], -1))} if best else None


# ---------------------------------------------------------------- output

def band_zh(v):
    if v is None:
        return "低於 45 dB 的建模門檻，屬「很安靜」"
    label = min(SCALE_ZH, key=lambda x: abs(v - x[0]))[1]  # nearest reference level
    return f"約 {v:.0f} dB，接近「{label}」"


def note_zh(r):
    s = "交通噪音" + band_zh(r["combined_db"])
    if r["max_50m_db"] is not None and (r["combined_db"] is None or r["max_50m_db"] - r["combined_db"] >= 5):
        s += f"，50 公尺內臨{MODE_ZH[r['max_50m_source']]}一側可達 {r['max_50m_db']:.0f} dB"
    elif r["max_50m_source"]:
        s += f"，主要來自{MODE_ZH[r['max_50m_source']]}"
    return s + "。"


def main():
    apts = json.loads((ROOT / "data" / "apartments.json").read_text())
    nr = NoiseRaster()
    hw, rail = osm_lines(apts)
    out = {}
    for a in apts:
        lat, lng = a["lat"], a["lng"]
        r = {f"{k}_db": db(nr.at(k, lat, lng)) for k in ("combined", "road", "aviation", "rail")}
        r["max_50m_db"] = db(nr.max_within("combined", lat, lng, RADIUS_M))
        near = {m: nr.max_within(m, lat, lng, RADIUS_M) for m in MODE_ZH}
        top = max(near, key=near.get)
        r["max_50m_source"] = top if db(near[top]) is not None else None  # loudest single source within 50 m
        r["below_threshold"] = [k for k in ("combined", "road", "aviation", "rail") if r[f"{k}_db"] is None]
        r["logan_contour"] = None  # no public GIS layer of Massport's DNL 65 contour found
        r["highway"] = nearest((lat, lng), hw)
        r["rail"] = nearest((lat, lng), rail)
        r["note_zh"] = note_zh(r)
        out[a["slug"]] = r
        print(f"{a['slug']:45s} {r['combined_db']} road={r['road_db']} air={r['aviation_db']} "
              f"rail={r['rail_db']} max50={r['max_50m_db']}", file=sys.stderr)

    probe = fetch(OVERLAY_SVC + "/tile/12/1517/1239", kind="bin")  # Boston tile, must be a PNG
    assert probe[:8] == b"\x89PNG\r\n\x1a\n", "overlay tile is not a PNG"
    doc = {
        "as_of": AS_OF,
        "source_name": "US DOT BTS National Transportation Noise Map 2020 (modeled 24-h LAeq, dBA); "
                       "float values via Esri Living Atlas 'USA Transportation Noise 2020'",
        "source_url": BTS_PAGE,
        "data_url": ESRI_ITEM,
        "year": 2020,
        "metric": "LAeq 24h, dBA, 30 m grid; value at the address point (pixel containing it)",
        "threshold_note": "BTS models noise only above ~45 dBA. null = below threshold for that source "
                          "(quieter), listed in below_threshold. Model ignores barriers, buildings, "
                          "helicopters, industry and ports. BTS's combined and per-source rasters are offset by about one "
                          "30 m pixel from each other in places (also visible in BTS's own map tiles), so at a "
                          "single point the combined value can be lower than one source; max_50m_db is the "
                          "more robust indicator next to busy roads/tracks.",
        "max_50m_note": "max_50m_db = highest combined value among 30 m pixels within 50 m of the point "
                        "(noisiest nearby frontage); max_50m_source = source (road/aviation/rail) with the "
                        "highest single-source value within the same 50 m.",
        "logan_note": "logan_contour is null: Massport publishes Logan DNL contours only as report maps, "
                      "no queryable GIS layer was found. Use aviation_db (BTS aviation LAeq) as the proxy.",
        "distance_note": "highway/rail: straight-line metres to the nearest OpenStreetMap way (not in tunnel); "
                         "highways = ways of the I-93, US 1, Route 2, I-90, Route 16, Route 1A route relations (ramps "
                         "excluded); rail = railway=rail/light_rail/subway running tracks (sidings, yards, "
                         "industrial/crane tracks excluded).",
        "scale_zh": SCALE_ZH,
        "overlay": {
            "type": "tile",
            "url": OVERLAY_SVC + "/tile/{z}/{y}/{x}",
            "attribution": "Noise: US DOT Bureau of Transportation Statistics (2020)",
            "note": "BTS 2020 combined aviation+rail+road, Web Mercator PNG tiles with transparency; "
                    "cached to z12 only, so use L.tileLayer(url, {maxNativeZoom: 12, maxZoom: 18, opacity: 0.6}). "
                    "Classes: 45-50 amber #FFC107, 50-55 orange #FF8000, 55-60 red #FF0000, "
                    "60-70 pink #FF3399, 70-80 purple #A300CC, 80-90 violet #5200CC, >90 blue #0000FF.",
        },
        "apartments": out,
    }
    OUT.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n")
    print(f"wrote {OUT}", file=sys.stderr)


if __name__ == "__main__":
    main()

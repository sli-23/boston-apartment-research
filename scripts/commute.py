"""Rapid-transit commute estimate (subway + Silver Line) from MBTA scheduled trips.

Builds a station graph from one representative weekday trip per route pattern, then runs
Dijkstra with walk-in / walk-out legs. Wait times are fixed estimates, so the result is
a typical door-to-door time, not a live trip plan.
"""
import heapq
import json
import math
from datetime import date, datetime, timedelta

from build_data import CACHE, fetch_mbta, haversine, walk

SILVER = ["741", "742", "743", "746", "749", "751"]
WAIT = {"rapid": 4, "silver": 6}  # minutes, applied per boarding (covers transfers too)
WALK_RADIUS = {"origin": 1500, "dest": 1300}
WALK_M_PER_MIN = 80 / 1.25  # crow distance → rough street distance at 4.8 km/h


def _routes():
    routes = fetch_mbta("/routes", **{"filter[type]": "0,1"}) + fetch_mbta("/routes", **{"filter[id]": ",".join(SILVER)})
    return {r["id"]: r["attributes"] for r in routes}


def _service_date():
    d = date.today()
    while d.weekday() >= 5:  # use the next weekday's timetable
        d += timedelta(days=1)
    return d.isoformat()


def _morning_trips(route_id, direction):
    """Distinct stop sequences (branches) for one route/direction in the 8am peak."""
    res = fetch_mbta("/schedules", full=True, **{"filter[route]": route_id, "filter[direction_id]": direction,
                                                 "filter[date]": _service_date(), "filter[min_time]": "08:00",
                                                 "filter[max_time]": "08:45", "include": "stop"})
    stops = {s["id"]: s for s in res.get("included", [])}
    trips = {}
    for sch in res["data"]:
        trips.setdefault(sch["relationships"]["trip"]["data"]["id"], []).append(sch)
    seen, out = set(), []
    for rows in sorted(trips.values(), key=len, reverse=True):
        rows.sort(key=lambda r: r["attributes"]["stop_sequence"])
        seq = []
        for sch in rows:
            s = stops.get(sch["relationships"]["stop"]["data"]["id"])
            if not s:
                continue
            parent = (s["relationships"].get("parent_station") or {}).get("data")
            t = sch["attributes"]["departure_time"] or sch["attributes"]["arrival_time"]
            seq.append({"id": parent["id"] if parent else s["id"], "name": s["attributes"]["name"],
                        "lat": s["attributes"]["latitude"], "lng": s["attributes"]["longitude"],
                        "arr": sch["attributes"]["arrival_time"] or t, "dep": t})
        key = tuple(x["id"] for x in seq)
        # skip partial trips that are a sub-run of a branch we already have
        if len(seq) < 2 or any(set(key) <= set(k) for k in seen):
            continue
        seen.add(key)
        out.append(seq)
    return out


def build_graph():
    path = CACHE / "transit_graph.json"
    if path.exists():
        return json.loads(path.read_text())
    routes, nodes, edges = _routes(), {}, []
    for rid in routes:
        for direction in (0, 1):
            for seq in _morning_trips(rid, direction):
                for s in seq:
                    nodes.setdefault(s["id"], {"name": s["name"], "lat": s["lat"], "lng": s["lng"]})
                for a, b in zip(seq, seq[1:]):
                    mins = (datetime.fromisoformat(b["arr"]) - datetime.fromisoformat(a["dep"])).total_seconds() / 60
                    edges.append([a["id"], b["id"], rid, max(0.5, mins)])
    graph = {"nodes": nodes, "edges": edges,
             "routes": {rid: {"name": a["long_name"] if a["type"] != 3 else a["short_name"],
                              "short": a["short_name"] or a["long_name"].replace(" Line", ""),
                              "color": "#" + a["color"], "text": "#" + a["text_color"],
                              "kind": "silver" if rid in SILVER else "rapid"} for rid, a in routes.items()}}
    path.write_text(json.dumps(graph))
    return graph


def _near(graph, pos, radius):
    return [(nid, haversine(pos, (n["lat"], n["lng"]))) for nid, n in graph["nodes"].items()
            if haversine(pos, (n["lat"], n["lng"])) <= radius]


def plan(origin, dest, graph):
    """Return legs + total minutes for the fastest walk/subway/walk combination."""
    adj = {}
    for u, v, r, t in graph["edges"]:
        adj.setdefault((u, r), []).append(((v, r), t))
    routes_at = {}
    for u, v, r, _ in graph["edges"]:
        routes_at.setdefault(u, set()).add(r)
    exits = dict((nid, d / WALK_M_PER_MIN) for nid, d in _near(graph, dest, WALK_RADIUS["dest"]))

    best_walk = haversine(origin, dest) / WALK_M_PER_MIN
    pq, dist, prev, tie = [], {}, {}, 0
    for nid, d in _near(graph, origin, WALK_RADIUS["origin"]):
        state = (nid, None)
        cost = d / WALK_M_PER_MIN
        if cost < dist.get(state, math.inf):
            dist[state] = cost
            tie += 1
            heapq.heappush(pq, (cost, tie, state))
    goal, goal_cost = None, best_walk
    while pq:
        cost, _, (node, route) = heapq.heappop(pq)
        if cost > dist.get((node, route), math.inf) or cost >= goal_cost:
            continue
        if route is None:
            if node in exits and cost + exits[node] < goal_cost:
                goal, goal_cost = node, cost + exits[node]
            nxt = [((node, r), WAIT[graph["routes"][r]["kind"]]) for r in routes_at.get(node, ())]
        else:
            nxt = adj.get((node, route), []) + [((node, None), 0)]
        for state, t in nxt:
            if cost + t < dist.get(state, math.inf):
                dist[state] = cost + t
                prev[state] = (node, route)
                tie += 1
                heapq.heappush(pq, (cost + t, tie, state))

    if goal is None:
        w = walk(origin, dest)
        return {"total": w["minutes"], "legs": [{"type": "walk", "minutes": w["minutes"], "meters": w["meters"], "line": w["line"], "to": "辦公室"}]}

    # rebuild station path
    path, state = [], (goal, None)
    while state in prev or state in dist:
        path.append(state)
        if state not in prev:
            break
        state = prev[state]
    path.reverse()
    nodes = graph["nodes"]
    first = nodes[path[0][0]]
    w_in = walk(origin, (first["lat"], first["lng"]))
    legs = [{"type": "walk", "minutes": w_in["minutes"], "meters": w_in["meters"], "line": w_in["line"], "to": first["name"]}]
    ride = None
    for (node, route), (nxt_node, nxt_route) in zip(path, path[1:]):
        if route is None and nxt_route is not None:  # boarding
            r = graph["routes"][nxt_route]
            ride = {"type": "ride", "route": nxt_route, "name": r["name"], "short": r["short"], "color": r["color"],
                    "text": r["text"], "from": nodes[node]["name"], "stops": 0, "minutes": 0.0,
                    "wait": WAIT[r["kind"]], "line": [[nodes[node]["lat"], nodes[node]["lng"]]]}
        elif route is not None and nxt_route == route:
            t = next(t for s, t in adj[(node, route)] if s == (nxt_node, route))
            ride["minutes"] += t
            ride["stops"] += 1
            ride["line"].append([nodes[nxt_node]["lat"], nodes[nxt_node]["lng"]])
        elif route is not None and nxt_route is None:  # alighting
            ride["to"] = nodes[node]["name"]
            ride["minutes"] = round(ride["minutes"])
            legs.append(ride)
    last = nodes[goal]
    w_out = walk((last["lat"], last["lng"]), dest)
    legs.append({"type": "walk", "minutes": w_out["minutes"], "meters": w_out["meters"], "line": w_out["line"], "to": "辦公室"})
    total = sum(l["minutes"] + l.get("wait", 0) for l in legs)
    if total > walk(origin, dest)["minutes"]:  # walking the whole way is faster
        w = walk(origin, dest)
        return {"total": w["minutes"], "legs": [{"type": "walk", "minutes": w["minutes"], "meters": w["meters"], "line": w["line"], "to": "辦公室"}]}
    return {"total": round(total), "legs": legs}

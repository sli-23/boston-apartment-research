"""Merge data/ai_picks*.json into data/apartments.json (flag ai=true) and run the per-apartment pipelines.

Usage: python3 -I scripts/add_ai_picks.py
Re-runnable: existing slugs are updated in place, not duplicated.
"""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# all AI pick files: ai_picks.json (general) + ai_picks_*.json (e.g. Jamaica Plain request)
picks = [p for f in sorted((ROOT / "data").glob("ai_picks*.json")) for p in json.loads(f.read_text())["picks"]]
apts_p = ROOT / "data" / "apartments.json"
apts = json.loads(apts_p.read_text())
by_slug = {a["slug"]: a for a in apts}
for p in picks:
    entry = {"slug": p["slug"], "name": p["name"], "address": p["address"], "lat": p["lat"], "lng": p["lng"],
             "ai": True, "why_zh": p.get("why_zh", []), "concerns_zh": p.get("concerns_zh", []), "built": p.get("built")}
    if p["slug"] in by_slug:
        by_slug[p["slug"]].update(entry)
    else:
        apts.append(entry)
apts_p.write_text(json.dumps(apts, indent=1, ensure_ascii=False))
print(f"apartments.json: {len(apts)} ({sum(1 for a in apts if a.get('ai'))} AI picks)")

py = [sys.executable, "-I"]
slugs = [p["slug"] for p in picks]
for cmd in (["scripts/areas.py"], ["scripts/build_data.py", *slugs], ["scripts/noise.py"], ["scripts/crime.py"]):
    print("→", " ".join(cmd), flush=True)
    subprocess.run(py + cmd, cwd=ROOT, check=False)

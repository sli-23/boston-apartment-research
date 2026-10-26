"""Render index.html like Chrome DevTools device mode (mobile viewport) and report problems.

Usage: python3 -I scripts/phone_check.py [hash] [out.png]
Uses --remote-debugging-pipe (no network port), real time (no virtual-time quirks).
"""
import base64
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


def _wire(r_in, w_out):
    # move both ends to high fds first so dup2 onto 3/4 can't collide, then clear close-on-exec
    hi_r, hi_w = os.dup(r_in) + 100, os.dup(w_out) + 100
    os.dup2(r_in, hi_r)
    os.dup2(w_out, hi_w)
    os.dup2(hi_r, 3)
    os.dup2(hi_w, 4)
    os.set_inheritable(3, True)
    os.set_inheritable(4, True)


class Pipe:
    def __init__(self):
        r_in, self.w = os.pipe()   # we write → chrome fd 3
        self.r, w_out = os.pipe()  # chrome fd 4 → we read
        self.proc = subprocess.Popen(
            [CHROME, "--headless=new", "--remote-debugging-pipe", "--no-first-run", "--hide-scrollbars",
             "--user-data-dir=/tmp/ba-phone-profile", "about:blank"],
            close_fds=False, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=open("/tmp/ba-phone-chrome.log", "w"),
            preexec_fn=lambda: _wire(r_in, w_out))
        os.close(r_in)
        os.close(w_out)
        self.buf, self.i, self.events = b"", 0, []

    def _read(self):
        while b"\0" not in self.buf:
            chunk = os.read(self.r, 1 << 20)
            if not chunk:
                raise RuntimeError("chrome closed the pipe")
            self.buf += chunk
        msg, self.buf = self.buf.split(b"\0", 1)
        return json.loads(msg)

    def call(self, method, session=None, **params):
        self.i += 1
        msg = {"id": self.i, "method": method, "params": params}
        if session:
            msg["sessionId"] = session
        os.write(self.w, json.dumps(msg).encode() + b"\0")
        while True:
            m = self._read()
            if m.get("id") == self.i:
                if "error" in m:
                    raise RuntimeError(m["error"])
                return m.get("result", {})
            self.events.append(m)


def main():
    frag = sys.argv[1] if len(sys.argv) > 1 else ""
    out = Path(sys.argv[2] if len(sys.argv) > 2 else ROOT / "shots" / "phone-real.png")
    # frag: "slug/tab", "?query#slug", or "guide.html#section"
    page, _, rest = frag.partition(".html") if ".html" in frag else ("index", "", frag)
    rest = rest if rest.startswith(("?", "#")) or not rest else "#" + rest
    url = (ROOT / f"{page}.html").as_uri() + rest
    p = Pipe()
    target = p.call("Target.createTarget", url="about:blank")["targetId"]
    s = p.call("Target.attachToTarget", targetId=target, flatten=True)["sessionId"]
    for d in ("Page.enable", "Runtime.enable", "Log.enable"):
        p.call(d, s)
    p.call("Emulation.setDeviceMetricsOverride", s, width=390, height=844, deviceScaleFactor=2, mobile=True)
    p.call("Emulation.setTouchEmulationEnabled", s, enabled=True, maxTouchPoints=5)
    p.call("Emulation.setUserAgentOverride", s, userAgent="Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
           "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")
    p.call("Page.navigate", s, url=url)
    time.sleep(9)  # real time for tiles
    probe = p.call("Runtime.evaluate", s, returnByValue=True, expression="""(() => {
      const r = e => { if (!e) return null; const b = e.getBoundingClientRect(); return [Math.round(b.left), Math.round(b.top), Math.round(b.width), Math.round(b.height)]; };
      const m = document.getElementById('map');
      const at = document.elementFromPoint(195, 150);
      return {vw: innerWidth, vh: innerHeight, sw: document.documentElement.scrollWidth, map: r(m),
              mapStyle: m && getComputedStyle(m).position + ' z=' + getComputedStyle(m).zIndex,
              tiles: document.querySelectorAll('.leaflet-tile-loaded').length,
              tilesAll: document.querySelectorAll('.leaflet-tile').length,
              topElement: at && (at.tagName + '.' + at.className + '#' + at.id),
              panel: r(document.getElementById('panel')),
              zoom: window.map && map.getZoom && map.getZoom(),
              cur: (typeof current !== 'undefined' && current) ? current.a.slug : null,
              center: window.map && map.getCenter && [map.getCenter().lat.toFixed(3), map.getCenter().lng.toFixed(3)]};
    })()""")["result"]["value"]
    if "/" in frag and ".html" not in frag:  # a tab was requested: scroll its content into view
        p.call("Runtime.evaluate", s, expression="document.querySelector('.tabs')?.scrollIntoView()")
        time.sleep(2)
    shot = p.call("Page.captureScreenshot", s, format="png")["data"]
    out.write_bytes(base64.b64decode(shot))
    errors = [e["params"] for e in p.events if e.get("method") in ("Runtime.exceptionThrown", "Log.entryAdded")
              and (e["method"] == "Runtime.exceptionThrown" or e["params"]["entry"]["level"] == "error")]
    print(json.dumps(probe, indent=1))
    for e in errors:
        print("ERROR:", json.dumps(e)[:300])
    print("screenshot:", out)
    p.proc.kill()


if __name__ == "__main__":
    main()

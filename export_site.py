"""Builds the hosted copy of the NQ Edge dashboard.

  python export_site.py page  <folder>   writes <folder>/index.html, a read-only page
  python export_site.py state <folder>   writes <folder>/state.json, the current numbers

The page is static. It loads state.json from the `data` branch of the GitHub repo, which the scan
workflow refreshes every 5 minutes. Run with NQ_EDGE_DATA pointing at the data folder.
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

# Runs before the dashboard's own script. It sends the page's "/api/state" request to the published
# state.json and answers every button press with a polite "read only" message.
SHIM = """
(function () {
  const params = new URLSearchParams(location.search);
  function stateUrl() {
    if (params.get("state")) return params.get("state");
    const owner = location.hostname.split(".")[0];
    const repo = location.pathname.split("/").filter(Boolean)[0] || "nq-edge";
    return "https://raw.githubusercontent.com/" + owner + "/" + repo + "/data/state.json";
  }
  const realFetch = window.fetch.bind(window);
  window.fetch = async function (path, opt) {
    if (typeof path === "string" && path.indexOf("/api/") === 0) {
      if (path === "/api/state") {
        const u = stateUrl();
        const r = await realFetch(u + (u.indexOf("?") < 0 ? "?" : "&") + "t=" + Date.now(), { cache: "no-store" });
        if (!r.ok) throw new Error("state " + r.status);
        return r;
      }
      return new Response(JSON.stringify({ ok: false,
        message: "This hosted copy is read only. Change the trade rules in the GitHub repository (config/paper_settings.json)." }),
        { status: 200, headers: { "Content-Type": "application/json" } });
    }
    return realFetch(path, opt);
  };
})();
"""

REPLACEMENTS = [
    ('const TOKEN = "__NQ_TOKEN__";', SHIM + '\nconst TOKEN = "hosted";'),
    ("Lost contact with the dashboard. Start it again with Start NQ Dashboard.bat, then reload this page.",
     "Could not load the latest numbers. Reload the page in a minute."),
    ("Lost contact with the dashboard. Start it again with Start NQ Dashboard.bat.",
     "Could not load the latest numbers."),
    ("The schedule tools only work on Windows.",
     "Scans run on GitHub every 5 minutes during the sessions. There is nothing to set up here."),
]


def build_page(folder):
    with open(os.path.join(HERE, "dashboard.html"), encoding="utf-8") as f:
        html = f.read()
    for old, new in REPLACEMENTS:
        if old not in html:
            raise SystemExit("dashboard.html changed: could not find text to replace: " + old[:60])
        html = html.replace(old, new)
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, "index.html"), "w", encoding="utf-8", newline="") as f:
        f.write(html)
    print("wrote", os.path.join(folder, "index.html"))


def build_state(folder):
    import nq_dashboard as d
    s = d.build_state()
    s["notify"] = {"topic": ""}          # the phone alert topic works like a password. Never publish it.
    s["schedule"] = {"tasks": [], "problems": [], "checked": None, "checking": False, "windows": False}
    os.makedirs(folder, exist_ok=True)
    tmp = os.path.join(folder, "state.json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(s, f, default=str, separators=(",", ":"))
    os.replace(tmp, os.path.join(folder, "state.json"))
    print("wrote", os.path.join(folder, "state.json"))


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] not in ("page", "state"):
        raise SystemExit(__doc__)
    (build_page if sys.argv[1] == "page" else build_state)(sys.argv[2])

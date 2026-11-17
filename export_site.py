"""Builds the hosted copy of the NQ Edge dashboard.

  python export_site.py page  <folder>   writes <folder>/index.html, a static page
  python export_site.py state <folder>   writes <folder>/state.json, the current numbers

The page is static. It loads state.json from the `data` branch of the GitHub repo, which the scan
workflow refreshes every 5 minutes. Run with NQ_EDGE_DATA pointing at the data folder.

A static page cannot run Python, so the dashboard's action buttons do the next best thing: they open
the matching GitHub page (Run workflow, the rules file, the data folder) in a new tab.
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

# Runs before the dashboard's own script. It sends the page's "/api/state" request to the published
# state.json and turns every button press into "open the right GitHub page".
SHIM = """
(function () {
  const params = new URLSearchParams(location.search);
  const owner = location.hostname.split(".")[0];
  const repo = location.pathname.split("/").filter(Boolean)[0] || "nq-edge";
  const gh = "https://github.com/" + owner + "/" + repo;
  function stateUrl() {
    if (params.get("state")) return params.get("state");
    return "https://raw.githubusercontent.com/" + owner + "/" + repo + "/data/state.json";
  }
  const RUN = gh + "/actions/workflows/scan.yml";
  const how = (what) => "Opening GitHub. Click Run workflow, choose '" + what +
    "', then press the green Run workflow button. This page updates about a minute later.";
  // path -> [page to open, message]
  const ROUTES = {
    "/api/scan": [RUN, how("scan")],
    "/api/analyze": [RUN, how("scan")],
    "/api/snapshot": [RUN, how("snapshot")],
    "/api/reactions": [RUN, how("refresh-all")],
    "/api/experiments": [RUN, how("refresh-all")],
    "/api/settings": [gh + "/edit/main/config/paper_settings.json",
      "Opening the rules file on GitHub. Change the numbers, then press Commit changes. New trades use them."],
    "/api/open-folder": [gh + "/tree/data/data", "Opening the data folder on GitHub."],
    "/api/notify": [gh + "/settings/secrets/actions",
      "Opening GitHub secrets. Add a secret named NTFY_TOPIC with your ntfy topic to get phone alerts."],
    "/api/repair": [null, "Not needed here. GitHub runs the scans."],
    "/api/refresh-schedule": [null, "Not needed here. GitHub runs the scans."]
  };
  const realFetch = window.fetch.bind(window);
  window.fetch = function (path, opt) {
    if (typeof path === "string" && path.indexOf("/api/") === 0) {
      if (path === "/api/state") {
        const u = stateUrl();
        return realFetch(u + (u.indexOf("?") < 0 ? "?" : "&") + "t=" + Date.now(), { cache: "no-store" })
          .then(function (r) { if (!r.ok) throw new Error("state " + r.status); return r; });
      }
      const route = ROUTES[path] || [null, "This button only works in the Windows version."];
      if (route[0]) window.open(route[0], "_blank", "noopener");   // runs inside the click, so it is not blocked
      return Promise.resolve(new Response(JSON.stringify({ ok: true, message: route[1] }),
        { status: 200, headers: { "Content-Type": "application/json" } }));
    }
    return realFetch(path, opt);
  };
})();
"""

HIDE = "#btn-repair, #btn-recheck { display: none !important; }\n"

REPLACEMENTS = [
    ('const TOKEN = "__NQ_TOKEN__";', SHIM + '\nconst TOKEN = "hosted";'),
    ("Lost contact with the dashboard. Start it again with Start NQ Dashboard.bat, then reload this page.",
     "Could not load the latest numbers. Reload the page in a minute."),
    ("Lost contact with the dashboard. Start it again with Start NQ Dashboard.bat.",
     "Could not load the latest numbers."),
    ("The schedule tools only work on Windows.",
     "Scans run on GitHub every 5 minutes during the sessions. There is nothing to set up here."),
    ("</style>", HIDE + "</style>"),
]


def build_page(folder):
    with open(os.path.join(HERE, "dashboard.html"), encoding="utf-8") as f:
        html = f.read()
    for old, new in REPLACEMENTS:
        if old not in html:
            raise SystemExit("dashboard.html changed: could not find text to replace: " + old[:60])
        html = html.replace(old, new, 1)
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

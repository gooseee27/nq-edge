"""NQ Edge dashboard (browser version).

Start it with:  py nq_dashboard.py   (or double-click 'Start NQ Dashboard.bat')
It opens a page in your browser at http://127.0.0.1:8765. Keep the command window open
while you use it. Only this computer can reach the page.
"""
import csv
import glob
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import traceback
import webbrowser
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import analyzer
import logger
import notify
import experiments
import paper
import reactions
import sessions

HERE = logger.HERE
DATA = logger.DATA
RUNLOG = logger.RUNLOG
HTML_PATH = os.path.join(HERE, "dashboard.html")
IS_WIN = os.name == "nt"
TOKEN = secrets.token_urlsafe(24)
RUN_LOCK = threading.Lock()

TASK_LABELS = {
    "NQ logger AM": "Morning snapshot",
    "NQ logger PM": "Afternoon snapshot",
    "NQ scan": "Paper trading scan",
    "NQ scan Asia": "Asia session scan",
}
ASIA_TASK = "NQ scan Asia"


# ---------------------------------------------------------------- data helpers
def fnum(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def read_rows():
    """All snapshots (current file, pending file, older-format files), oldest first."""
    paths = [logger.SNAP, logger.PENDING] + sorted(glob.glob(os.path.join(DATA, "snapshots_old_*.csv")))
    rows, seen = [], set()
    for path in paths:
        if not os.path.exists(path):
            continue
        try:
            with open(path, newline="", encoding="utf-8-sig") as f:
                for r in csv.DictReader(f):
                    ts = r.get("ts_utc")
                    if not ts or ts in seen:
                        continue
                    try:
                        dt = datetime.fromisoformat(ts).astimezone()
                    except ValueError:
                        continue
                    seen.add(ts)
                    r["_dt"] = dt
                    rows.append(r)
        except OSError:
            continue
    rows.sort(key=lambda r: r["_dt"])
    return rows


def tail(path, n=40):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read().splitlines()[-n:]
    except OSError:
        return []


def json_list(text):
    try:
        v = json.loads(text or "[]")
        return v if isinstance(v, list) else []
    except ValueError:
        return []


def load_bars(limit=1000):
    """Newest 1 minute bars on disk, as [[utc_iso, close], ...]."""
    files = glob.glob(os.path.join(logger.BARS, "*.csv"))
    if not files:
        return [], None
    path = max(files, key=os.path.getmtime)
    pts = []
    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            rd = csv.reader(f)
            header = next(rd, None)
            if not header:
                return [], None
            cols = [h.strip().lower() for h in header]
            if "close" not in cols:
                return [], None
            ci = cols.index("close")
            for row in rd:
                try:
                    dt = datetime.fromisoformat(row[0].strip())
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    pts.append([dt.astimezone(timezone.utc).isoformat(timespec="seconds"), round(float(row[ci]), 2)])
                except (ValueError, IndexError):
                    continue
    except OSError:
        return [], None
    updated = datetime.fromtimestamp(os.path.getmtime(path)).astimezone().isoformat(timespec="seconds")
    return pts[-limit:], updated


# ---------------------------------------------------------------- Windows schedule helpers
def ps(script, timeout=90):
    if not IS_WIN:
        return 1, "", "The schedule tools only work on Windows."
    p = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True, text=True, timeout=timeout, creationflags=0x08000000,
    )
    return p.returncode, p.stdout, p.stderr


def q(s):
    """Quote a string for PowerShell."""
    return "'" + s.replace("'", "''") + "'"


STATUS_PS = r"""
$ErrorActionPreference = 'SilentlyContinue'
$out = @()
foreach ($n in 'NQ logger AM', 'NQ logger PM', 'NQ scan', 'NQ scan Asia') {
  $t = Get-ScheduledTask -TaskName $n
  if ($t) {
    $i = Get-ScheduledTaskInfo -TaskName $n
    $last = ''
    if ($i.LastRunTime -and $i.LastRunTime.Year -gt 2000) { $last = $i.LastRunTime.ToString('yyyy-MM-ddTHH:mm:ss') }
    $next = ''
    if ($i.NextRunTime -and $i.NextRunTime.Year -gt 2000) { $next = $i.NextRunTime.ToString('yyyy-MM-ddTHH:mm:ss') }
    $out += [pscustomobject]@{
      Name = $n; Exists = $true; State = [string]$t.State; Last = $last; Next = $next
      Result = [int64]$i.LastTaskResult
      OnBattery = (-not $t.Settings.DisallowStartIfOnBatteries)
      CatchUp = [bool]$t.Settings.StartWhenAvailable
      Wake = [bool]$t.Settings.WakeToRun
    }
  } else {
    $out += [pscustomobject]@{ Name = $n; Exists = $false }
  }
}
ConvertTo-Json -InputObject @($out) -Compress
"""


def task_status():
    """Returns (list_of_task_dicts, error_text)."""
    code, out, err = ps(STATUS_PS)
    if code != 0 and not out.strip():
        return [], (err.strip() or "Could not read the schedule.")
    try:
        data = json.loads(out)
    except ValueError:
        return [], "Could not read the schedule: " + (err.strip() or out.strip())[:200]
    if isinstance(data, dict):
        data = [data]
    return data, ""


def python_for_tasks():
    exe = sys.executable
    w = os.path.join(os.path.dirname(exe), "pythonw.exe")  # no console window flash
    return w if os.path.exists(w) else exe


def scan_task_lines(asia_on):
    """PowerShell lines that (re)create the paper trading scan tasks. Needs $exe and $dir to be set.

    The windows are wide on purpose. Windows tasks follow this computer's clock (Arizona has no daylight
    saving) while the trading sessions follow New York time, which shifts an hour on the first Sunday of
    November and in March. The scan itself only trades inside the real session times, so extra runs
    around the edges do nothing.
      US scan    06:50 for 7.5 hours (covers 9:45 AM to 3:55 PM New York time in both seasons)
      Asia scan  17:10 for 7 hours (covers 8:15 PM to 1:55 AM New York time in both seasons)
    """
    rep = ("-RepetitionInterval (New-TimeSpan -Minutes 5) -RepetitionDuration (New-TimeSpan -Hours {h} -Minutes {m})")
    lines = [
        f"$scan = {q(os.path.join(HERE, 'scan.py'))}",
        "$sset = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries "
        "-StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 4)",
        "$sa = New-ScheduledTaskAction -Execute $exe -Argument ('\"' + $scan + '\"') -WorkingDirectory $dir",
        "$days = 'Monday','Tuesday','Wednesday','Thursday','Friday'",
        "$st = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $days -At ([datetime]('06:50'))",
        "$st.Repetition = (New-ScheduledTaskTrigger -Once -At ([datetime]('06:50')) " + rep.format(h=7, m=30) + ").Repetition",
        "Register-ScheduledTask -TaskName 'NQ scan' -Action $sa -Trigger $st -Settings $sset -Force | Out-Null",
    ]
    if asia_on:
        lines += [
            "$adays = 'Sunday','Monday','Tuesday','Wednesday','Thursday'",
            "$at = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $adays -At ([datetime]('17:10'))",
            "$at.Repetition = (New-ScheduledTaskTrigger -Once -At ([datetime]('17:10')) " + rep.format(h=7, m=0) + ").Repetition",
            f"Register-ScheduledTask -TaskName {q(ASIA_TASK)} -Action $sa -Trigger $at -Settings $sset -Force | Out-Null",
        ]
    else:
        lines.append(f"Unregister-ScheduledTask -TaskName {q(ASIA_TASK)} -Confirm:$false -ErrorAction SilentlyContinue")
    return lines


def sync_asia_task(asia_on):
    """Add or remove only the Asia scan task. Returns (ok, message)."""
    lines = ["$ErrorActionPreference = 'Stop'", f"$exe = {q(python_for_tasks())}", f"$dir = {q(HERE)}"] + scan_task_lines(asia_on)
    code, out, err = ps("\n".join(lines + ["Write-Output 'done'"]))
    if code == 0 and "done" in out:
        return True, ""
    return False, (err.strip() or out.strip() or "unknown error")


def repair_schedule(am, pm, wake):
    """Create or replace the tasks with settings that suit a laptop. Returns (ok, message)."""
    for t in (am, pm):
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", t):
            return False, f"'{t}' is not a valid time. Use 24 hour HH:MM, for example 06:45 or 12:30."
    asia_on = sessions.asia_on(paper.load_settings())
    script = "\n".join([
        "$ErrorActionPreference = 'Stop'",
        f"$exe = {q(python_for_tasks())}",
        f"$script = {q(os.path.join(HERE, 'logger.py'))}",
        f"$dir = {q(HERE)}",
        "$days = 'Monday','Tuesday','Wednesday','Thursday','Friday'",
        "$set = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries "
        "-StartWhenAvailable " + ("-WakeToRun " if wake else "") + "-ExecutionTimeLimit (New-TimeSpan -Minutes 15)",
        f"$jobs = @(@('NQ logger AM', {q(am)}), @('NQ logger PM', {q(pm)}))",
        "foreach ($j in $jobs) {",
        "  $a = New-ScheduledTaskAction -Execute $exe -Argument ('\"' + $script + '\"') -WorkingDirectory $dir",
        "  $t = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $days -At ([datetime]($j[1]))",
        "  Register-ScheduledTask -TaskName $j[0] -Action $a -Trigger $t -Settings $set -Force | Out-Null",
        "}",
    ] + scan_task_lines(asia_on) + ["Write-Output 'done'"])
    code, out, err = ps(script)
    if code == 0 and "done" in out:
        return True, ("Schedule saved. Snapshots run twice a day on weekdays. The paper trading scan runs every 5 minutes "
                      "during the US day" + (" and the Asia session (Sunday to Thursday evenings)" if asia_on else "") +
                      ". Everything runs on battery and catches up after the laptop wakes, but the laptop has to be on "
                      "for scans to run.")
    return False, "Windows would not save the schedule: " + (err.strip() or out.strip() or "unknown error")


SCHEDULE = {"tasks": [], "error": "", "checked": None, "checking": False}


def refresh_schedule():
    if SCHEDULE["checking"]:
        return
    SCHEDULE["checking"] = True

    def work():
        try:
            tasks, err = task_status()
            SCHEDULE.update(tasks=tasks, error=err,
                            checked=datetime.now().astimezone().isoformat(timespec="seconds"))
        except Exception as e:  # noqa: BLE001
            SCHEDULE.update(tasks=[], error=str(e))
        finally:
            SCHEDULE["checking"] = False

    threading.Thread(target=work, daemon=True).start()


def schedule_view():
    tasks_out, problems = [], []
    err = SCHEDULE["error"]
    if err:
        problems.append(err)
    asia_on = sessions.asia_on(paper.load_settings())
    for t in SCHEDULE["tasks"]:
        label = TASK_LABELS.get(t.get("Name"), t.get("Name"))
        if not t.get("Exists"):
            if t.get("Name") == ASIA_TASK and not asia_on:
                continue                      # Asia is switched off, so there is nothing to set up
            tasks_out.append({"name": label, "exists": False})
            problems.append(f"{label} is not set up.")
            continue
        code = int(t.get("Result") or 0)
        state = "ok" if code == 0 else "never" if code == 267011 else "running" if code == 267009 else "failed"
        if not t.get("OnBattery"):
            problems.append(f"{label} will not start on battery.")
        if not t.get("CatchUp"):
            problems.append(f"{label} will not catch up after the laptop sleeps.")
        tasks_out.append({
            "name": label, "exists": True, "last": t.get("Last") or "", "next": t.get("Next") or "",
            "result": state, "code": f"0x{code & 0xFFFFFFFF:08X}" if state == "failed" else "",
            "onBattery": bool(t.get("OnBattery")), "catchUp": bool(t.get("CatchUp")),
        })
    if not SCHEDULE["tasks"] and not err and SCHEDULE["checked"]:
        problems.append("No schedule found yet.")
    return {"tasks": tasks_out, "problems": problems, "checked": SCHEDULE["checked"],
            "checking": SCHEDULE["checking"], "windows": IS_WIN}


def schedule_loop():
    while True:
        threading.Event().wait(300)
        refresh_schedule()


# ---------------------------------------------------------------- state for the page
def build_state():
    rows = read_rows()
    latest = None
    if rows:
        r = rows[-1]
        latest = {
            "ts": r["_dt"].isoformat(timespec="seconds"),
            "nq": fnum(r.get("nq_price")), "regime": r.get("regime") or "",
            "callWall": fnum(r.get("call_wall")), "putWall": fnum(r.get("put_wall")),
            "flip": fnum(r.get("gamma_flip")), "flipNote": r.get("flip_note") or "",
            "strikes": [v for v in (fnum(x) for x in json_list(r.get("top_strikes"))) if v is not None],
            "deltaZ": fnum(r.get("delta_z")), "absorption": r.get("absorption") == "True",
            "divergence": r.get("divergence") or "", "netGex": fnum(r.get("net_gex_bn")),
            "setups": json_list(r.get("setups")),
        }
    now_utc = datetime.now(timezone.utc)
    kind = sessions.levels_kind(now_utc)
    levels = None
    if latest:
        levels = dict(latest, kind="day", fallback=False)
        if kind == "overnight":
            orow = paper.levels_row("overnight", now_utc)
            if orow:
                lv = paper.row_to_levels(orow)
                levels = {"kind": "overnight", "fallback": False, "ts": orow.get("ts_utc"), "nq": fnum(orow.get("nq_price")),
                          "regime": lv["regime"], "callWall": lv["call_wall"], "putWall": lv["put_wall"],
                          "flip": lv["gamma_flip"], "flipNote": orow.get("flip_note") or "", "strikes": lv["top_strikes"],
                          "netGex": fnum(orow.get("net_gex_bn"))}
            else:
                levels["kind"], levels["fallback"] = "overnight", True   # none saved yet: show the day levels, flagged
    snaps = [{
        "ts": r["_dt"].isoformat(timespec="seconds"), "nq": fnum(r.get("nq_price")),
        "regime": r.get("regime") or "", "flip": fnum(r.get("gamma_flip")),
        "call": fnum(r.get("call_wall")), "put": fnum(r.get("put_wall")),
        "dz": fnum(r.get("delta_z")), "setups": len(json_list(r.get("setups"))),
    } for r in reversed(rows[-150:])]

    counts = {}
    for r in rows:
        counts[r["_dt"].date()] = counts.get(r["_dt"].date(), 0) + 1
    days, d = [], datetime.now().date()
    while len(days) < 14:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    days.reverse()
    today = datetime.now().date()
    calendar = [{"date": x.isoformat(), "count": counts.get(x, 0), "today": x == today} for x in days]

    bars, bars_updated = load_bars()
    trades = []
    raw = paper.read_trades()
    for t in raw[-200:]:
        trades.append({
            "id": t.get("id"), "opened": t.get("opened_utc"), "side": t.get("side"), "level": t.get("level"),
            "entry": fnum(t.get("entry")), "stop": fnum(t.get("stop")), "target": fnum(t.get("target")),
            "status": t.get("status"), "closed": t.get("closed_utc") or None, "exit": fnum(t.get("exit")),
            "reason": t.get("reason") or "", "pts": fnum(t.get("pnl_pts")), "usd": fnum(t.get("pnl_usd")),
            "why": t.get("why") or "", "session": t.get("session") or sessions.US,
            "profile": paper.profile_of(t), "kind": t.get("kind") or "bounce",
        })
    s = paper.load_settings()
    nxt = None if sessions.session_at(now_utc, s) else sessions.next_window(now_utc, s)
    cur = sessions.session_at(now_utc, s)
    session = {"name": cur["name"] if cur else None, "label": sessions.LABELS[cur["name"]] if cur else None,
               "next": nxt["text"] if nxt else None,
               "nextStart": nxt["start_utc"].isoformat() if nxt else None, "describe": sessions.describe(s), "asiaOn": sessions.asia_on(s)}
    return {
        "now": datetime.now().astimezone().isoformat(timespec="seconds"),
        "latest": latest, "levels": levels, "session": session, "notify": {"topic": notify.get_topic()}, "reactions": reactions.read(), "experiments": experiments.load(), "snapshots": snaps, "snapshotCount": len(rows), "calendar": calendar,
        "bars": bars, "barsUpdated": bars_updated,
        "paper": {"trades": trades, "stats": paper.stats(raw),
                  "settings": {"stop": s["stop_pts"], "target": s["target_pts"], "minScore": s["min_score"],
                               "pointValue": s["point_value"],
                               "asia": {"enabled": sessions.asia_on(s), "stop": s["asia_stop_pts"],
                                        "target": s["asia_target_pts"], "minScore": s["asia_min_score"]},
                               "loose": {"enabled": bool(s["loose_enabled"]), "deltaZ": s["loose_delta_z"], "proxMult": s["loose_prox_mult"]},
                               "retest": bool(s["retest_enabled"])}},
        "log": tail(RUNLOG), "schedule": schedule_view(), "live": analyzer.read_live(),
    }


# ---------------------------------------------------------------- actions
def act_snapshot(_body):
    row, msg = logger.run_snapshot(demo=False)
    logger.log_line("OK (dashboard) " + msg)
    return msg


def act_scan(_body):
    msg, changed = paper.scan_once()
    if changed:
        logger.log_line("PAPER (dashboard) " + msg)
    return msg


def act_analyze(_body):
    d = analyzer.run_now()
    return "Market read updated. " + d["verdict"]["title"] + "."


def act_repair(body):
    ok, msg = repair_schedule(str(body.get("am", "")).strip(), str(body.get("pm", "")).strip(),
                              bool(body.get("wake", True)))
    refresh_schedule()
    if not ok:
        raise RuntimeError(msg)
    return msg


def act_settings(body):
    try:
        stop, target = float(body["stop"]), float(body["target"])
        score, pv = int(body["minScore"]), float(body["pointValue"])
    except (KeyError, ValueError, TypeError):
        raise RuntimeError("Enter numbers for stop, target and minimum score.")
    if stop <= 0 or target <= 0 or not 1 <= score <= 5 or pv not in (2.0, 20.0):
        raise RuntimeError("Stop and target must be above zero, and the minimum score between 1 and 5.")
    s = paper.load_settings()
    was_on = sessions.asia_on(s)
    s.update(stop_pts=stop, target_pts=target, min_score=score, point_value=pv)
    asia = body.get("asia")
    if isinstance(asia, dict):
        try:
            a_stop, a_target, a_score = float(asia["stop"]), float(asia["target"]), int(asia["minScore"])
        except (KeyError, ValueError, TypeError):
            raise RuntimeError("Enter numbers for the Asia stop, target and minimum score.")
        if a_stop <= 0 or a_target <= 0 or not 1 <= a_score <= 5:
            raise RuntimeError("The Asia stop and target must be above zero, and its minimum score between 1 and 5.")
        s.update(asia_enabled=bool(asia.get("enabled")), asia_stop_pts=a_stop, asia_target_pts=a_target,
                 asia_min_score=a_score)
    loose = body.get("loose")
    if isinstance(loose, dict):
        try:
            lz, lm = float(loose["deltaZ"]), float(loose["proxMult"])
        except (KeyError, ValueError, TypeError):
            raise RuntimeError("Enter numbers for the loose rules.")
        if not 0.3 <= lz <= 3 or not 1 <= lm <= 4:
            raise RuntimeError("Loose flow strength must be between 0.3 and 3, and the distance multiplier between 1 and 4.")
        s.update(loose_enabled=bool(loose.get("enabled")), loose_delta_z=lz, loose_prox_mult=lm)
    if "retest" in body:
        s.update(retest_enabled=bool(body.get("retest")))
    paper.save_settings(s)
    msg = "Trade rules saved. They apply to new trades. Open trades keep their old stop and target."
    now_on = sessions.asia_on(s)
    if IS_WIN and now_on != was_on:     # keep the Windows schedule in step with the Asia switch
        ok, err = sync_asia_task(now_on)
        refresh_schedule()
        msg += (" The Asia scan was " + ("added to" if now_on else "removed from") + " the schedule.") if ok else \
               (" Windows would not change the Asia scan task (" + err[:160] + "). Click Create or repair schedule.")
    elif now_on != was_on:
        msg += " Asia is now " + ("on" if now_on else "off") + "."
    return msg


def act_notify(body):
    topic = str(body.get("topic") or "").strip()
    if body.get("generate"):
        topic = notify.new_topic()
    try:
        notify.set_topic(topic)
    except ValueError as e:
        raise RuntimeError(str(e))
    if not topic:
        return "Phone alerts are off."
    ok, err = notify.send("NQ Edge test", "Phone alerts are working. You will get a message when a paper trade opens or closes.", topic)
    if not ok:
        raise RuntimeError("Saved, but the test message did not go out: " + err)
    return "Saved. A test message was sent to topic " + topic + "."


def act_experiments(_body):
    d = experiments.run()
    n = sum((r or {}).get("trades", 0) for r in d["results"].values())
    return f"Experiments updated. {n} trades since they were locked."


def act_reactions(_body):
    r = reactions.run()
    return r["verdict"]["title"] + "."


def act_refresh(_body):
    refresh_schedule()
    return "Checking the schedule."


def act_folder(_body):
    os.makedirs(DATA, exist_ok=True)
    if IS_WIN:
        os.startfile(DATA)  # noqa: S606
        return "Opened the data folder."
    return "Data folder: " + DATA


ACTIONS = {
    "/api/snapshot": (act_snapshot, True),   # (function, needs the run lock)
    "/api/scan": (act_scan, True),
    "/api/analyze": (act_analyze, True),
    "/api/reactions": (act_reactions, True),
    "/api/experiments": (act_experiments, True),
    "/api/repair": (act_repair, False),
    "/api/settings": (act_settings, False),
    "/api/refresh-schedule": (act_refresh, False),
    "/api/notify": (act_notify, False),
    "/api/open-folder": (act_folder, False),
}


# ---------------------------------------------------------------- web server
class Handler(BaseHTTPRequestHandler):
    server_version = "NQEdge"

    def log_message(self, *args):
        pass

    def _host_ok(self):
        port = self.server.server_port
        return (self.headers.get("Host") or "").lower() in (f"127.0.0.1:{port}", f"localhost:{port}")

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, default=str))

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, "Forbidden", "text/plain")
        path = self.path.split("?")[0]
        if path == "/":
            try:
                with open(HTML_PATH, encoding="utf-8") as f:
                    page = f.read().replace("__NQ_TOKEN__", TOKEN)
            except OSError:
                return self._send(500, "dashboard.html is missing. Put it in the same folder as nq_dashboard.py.",
                                  "text/plain; charset=utf-8")
            return self._send(200, page, "text/html; charset=utf-8")
        if path == "/api/state":
            if self.headers.get("X-NQ-Token") != TOKEN:
                return self._json({"ok": False, "message": "Reload the page."}, 403)
            try:
                return self._json(build_state())
            except Exception as e:  # noqa: BLE001
                logger.log_line("DASHBOARD STATE FAILED " + repr(e) + "\n" + traceback.format_exc())
                return self._json({"ok": False, "message": str(e)}, 500)
        return self._send(404, "Not found", "text/plain")

    def do_POST(self):
        if not self._host_ok() or self.headers.get("X-NQ-Token") != TOKEN:
            return self._json({"ok": False, "message": "Reload the page and try again."}, 403)
        path = self.path.split("?")[0]
        if path not in ACTIONS:
            return self._json({"ok": False, "message": "Unknown action."}, 404)
        try:
            n = min(int(self.headers.get("Content-Length") or 0), 10_000)
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            body = {}
        func, locked = ACTIONS[path]
        if locked and not RUN_LOCK.acquire(blocking=False):
            return self._json({"ok": False, "message": "Another snapshot or scan is still running. Try again in a moment."}, 409)
        try:
            msg = func(body if isinstance(body, dict) else {})
            return self._json({"ok": True, "message": msg})
        except Exception as e:  # noqa: BLE001
            if path in ("/api/snapshot", "/api/scan", "/api/analyze", "/api/reactions", "/api/experiments"):
                logger.log_line("FAILED (dashboard) " + repr(e) + "\n" + traceback.format_exc())
            return self._json({"ok": False, "message": str(e) or e.__class__.__name__}, 200)
        finally:
            if locked:
                RUN_LOCK.release()


def main():
    os.makedirs(DATA, exist_ok=True)
    if not os.path.exists(HTML_PATH):
        print("dashboard.html is missing. Put it in the same folder as nq_dashboard.py, then start again.")
        input("Press Enter to close.")
        return
    srv = None
    for port in range(8765, 8785):
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            continue
    if srv is None:
        print("Could not find a free port between 8765 and 8784. Close other copies of the dashboard and try again.")
        input("Press Enter to close.")
        return
    url = f"http://127.0.0.1:{srv.server_port}"
    refresh_schedule()
    threading.Thread(target=schedule_loop, daemon=True).start()
    print(f"NQ Edge is running at {url}")
    print("Your browser should open by itself. Keep this window open while you use the dashboard.")
    print("Close this window (or press Ctrl+C) to stop it. Scheduled snapshots keep running either way.")
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

"""Phone alerts through ntfy.sh (free, no account). Install the ntfy app, subscribe to your topic name,
and every paper trade open or close is pushed to your phone.

The topic name works like a password: anyone who knows it can read your alerts, so keep it random.
Nothing is sent until a topic is saved (dashboard, Automation, Phone alerts).
"""
import json
import os
import re
import secrets
import urllib.request

import logger

CFG = os.path.join(logger.DATA, "notify.json")
TOPIC_RE = re.compile(r"[A-Za-z0-9_-]{8,64}")


def new_topic():
    return "nqedge-" + secrets.token_urlsafe(9)


def get_topic():
    env = (os.environ.get("NTFY_TOPIC") or "").strip()   # on GitHub the topic is a secret, not a file
    if TOPIC_RE.fullmatch(env):
        return env
    try:
        with open(CFG, encoding="utf-8") as f:
            t = str(json.load(f).get("topic") or "")
        return t if TOPIC_RE.fullmatch(t) else ""
    except (OSError, ValueError, AttributeError):
        return ""


def set_topic(topic):
    topic = (topic or "").strip()
    if topic and not TOPIC_RE.fullmatch(topic):
        raise ValueError("The topic can only use letters, numbers, - and _, 8 to 64 characters.")
    os.makedirs(logger.DATA, exist_ok=True)
    with open(CFG, "w", encoding="utf-8") as f:
        json.dump({"topic": topic}, f)


def send(title, message, topic=None):
    """Returns (ok, error). Never raises. Does nothing (ok) when no topic is saved."""
    topic = topic or get_topic()
    if not topic:
        return True, ""
    try:
        req = urllib.request.Request("https://ntfy.sh/" + topic, data=message.encode("utf-8"), method="POST",
                                     headers={"Title": title, "Priority": "high", "Tags": "chart_with_upwards_trend"})
        with urllib.request.urlopen(req, timeout=8) as r:
            return (200 <= r.status < 300), ""
    except Exception as e:  # noqa: BLE001
        return False, str(e)[:200]

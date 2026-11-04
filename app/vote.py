"""A tiny live audience poll, served by Apache mod_wsgi.

Standard library only, so the web VM needs nothing beyond python3 and mod_wsgi.
Routes are relative to wherever the app is mounted (``/vote`` in production):

    GET  /              voting page for phones
    GET  /results       projector page: live bars, timeline, QR code
    GET  /qrcode.js     vendored QR code generator
    GET  /api/me        this phone's current answer
    POST /api/vote      {"choice": "yes"|"no"}
    GET  /api/results   running totals and timeline
"""

import json
import os
import secrets
import sqlite3
import threading
import time
from http import cookies

HERE = os.path.dirname(os.path.abspath(__file__))
QUESTION = "I liked James’s Talk"
CHOICES = ("yes", "no")
COOKIE = "vote_id"
TIMELINE_POINTS = 120

_lock = threading.Lock()
_connections = {}


def db(path):
    """One connection per database file per process, guarded by _lock."""
    conn = _connections.get(path)
    if conn is None:
        conn = sqlite3.connect(path, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS votes (
                voter TEXT PRIMARY KEY, choice TEXT NOT NULL, ts REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY, ts REAL NOT NULL,
                voter TEXT NOT NULL, choice TEXT NOT NULL);
            """
        )
        conn.commit()
        _connections[path] = conn
    return conn


def cast(conn, voter, choice, now=None):
    if choice not in CHOICES:
        raise ValueError(choice)
    now = time.time() if now is None else now
    with _lock:
        row = conn.execute("SELECT choice FROM votes WHERE voter=?", (voter,)).fetchone()
        if row and row[0] == choice:
            return
        conn.execute(
            "INSERT INTO votes(voter, choice, ts) VALUES(?,?,?) "
            "ON CONFLICT(voter) DO UPDATE SET choice=excluded.choice, ts=excluded.ts",
            (voter, choice, now),
        )
        conn.execute(
            "INSERT INTO events(ts, voter, choice) VALUES(?,?,?)", (now, voter, choice)
        )
        conn.commit()


def current(conn, voter):
    with _lock:
        row = conn.execute("SELECT choice FROM votes WHERE voter=?", (voter,)).fetchone()
    return row[0] if row else None


def results(conn):
    with _lock:
        totals = dict(conn.execute("SELECT choice, COUNT(*) FROM votes GROUP BY choice"))
        events = conn.execute("SELECT ts, voter, choice FROM events ORDER BY id").fetchall()
    # Replay the events to get the running totals after each one.
    state, counts, timeline = {}, {c: 0 for c in CHOICES}, []
    for ts, voter, choice in events:
        if voter in state:
            counts[state[voter]] -= 1
        state[voter] = choice
        counts[choice] += 1
        timeline.append([round(ts, 1), counts["yes"], counts["no"]])
    if len(timeline) > TIMELINE_POINTS:
        step = len(timeline) / TIMELINE_POINTS
        timeline = [timeline[int(i * step)] for i in range(TIMELINE_POINTS - 1)] + [timeline[-1]]
    return {
        "question": QUESTION,
        "yes": totals.get("yes", 0),
        "no": totals.get("no", 0),
        "timeline": timeline,
        "now": round(time.time(), 1),
    }


def _read(name):
    with open(os.path.join(HERE, name), "rb") as f:
        return f.read()


def application(environ, start_response):
    path = environ.get("PATH_INFO", "") or "/"
    method = environ.get("REQUEST_METHOD", "GET")
    base = environ.get("SCRIPT_NAME", "")
    conn = db(environ.get("VOTE_DB") or os.environ.get("VOTE_DB") or os.path.join(HERE, "vote.sqlite"))

    jar = cookies.SimpleCookie(environ.get("HTTP_COOKIE", ""))
    voter = jar[COOKIE].value if COOKIE in jar and len(jar[COOKIE].value) == 32 else None
    headers = [("Cache-Control", "no-store")]
    if voter is None:
        voter = secrets.token_hex(16)
        secure = "; Secure" if environ.get("HTTP_X_FORWARDED_PROTO", environ.get("wsgi.url_scheme")) == "https" else ""
        headers.append(
            ("Set-Cookie", f"{COOKIE}={voter}; Path={base or '/'}; Max-Age=31536000; HttpOnly; SameSite=Lax{secure}")
        )

    def respond(status, body, ctype):
        if isinstance(body, str):
            body = body.encode()
        start_response(status, headers + [("Content-Type", ctype), ("Content-Length", str(len(body)))])
        return [body]

    def as_json(obj, status="200 OK"):
        return respond(status, json.dumps(obj), "application/json")

    if method == "GET" and path == "/":
        page = _read("vote.html").decode().replace("{{QUESTION}}", QUESTION).replace("{{BASE}}", base)
        return respond("200 OK", page, "text/html; charset=utf-8")
    if method == "GET" and path in ("/results", "/results/"):
        page = _read("results.html").decode().replace("{{QUESTION}}", QUESTION).replace("{{BASE}}", base)
        return respond("200 OK", page, "text/html; charset=utf-8")
    if method == "GET" and path == "/qrcode.js":
        return respond("200 OK", _read("qrcode.js"), "application/javascript")
    if method == "GET" and path == "/api/me":
        return as_json({"choice": current(conn, voter)})
    if method == "GET" and path == "/api/results":
        return as_json(results(conn))
    if method == "POST" and path == "/api/vote":
        try:
            length = min(int(environ.get("CONTENT_LENGTH") or 0), 1000)
            choice = json.loads(environ["wsgi.input"].read(length) or b"{}").get("choice")
            cast(conn, voter, choice)
        except (ValueError, AttributeError):
            return as_json({"error": "choice must be yes or no"}, "400 Bad Request")
        return as_json({"choice": choice})
    return as_json({"error": "not found"}, "404 Not Found")

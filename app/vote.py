"""A tiny live audience poll, served by Apache mod_wsgi.

Standard library only, so the web VM needs nothing beyond python3 and mod_wsgi.
Routes are relative to wherever the app is mounted (``/vote`` in production):

    GET  /              voting page for phones
    GET  /results       projector page: live bars, timeline, QR code
    GET  /qrcode.js     vendored QR code generator
    GET  /ucl-logo.svg  UCL's own logo for dark backgrounds
    GET  /api/me        this phone's current answer
    POST /api/vote      {"choice": "yes"|"no"}
    POST /api/comment   {"text": "..."} shown on the results screen
    GET  /api/results   running totals and timeline
    GET  /api/comments  every comment, oldest first (text and time only)
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
COMMENT_LENGTH = 140
COMMENT_GAP = 10  # seconds between comments from one phone
COMMENTS_SHOWN = 30

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
            CREATE TABLE IF NOT EXISTS comments (
                id INTEGER PRIMARY KEY, ts REAL NOT NULL,
                voter TEXT NOT NULL, text TEXT NOT NULL);
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


class TooSoon(Exception):
    pass


def comment(conn, voter, text, now=None):
    if not isinstance(text, str):
        raise ValueError(text)
    text = " ".join(text.split())[:COMMENT_LENGTH]
    if not text:
        raise ValueError(text)
    now = time.time() if now is None else now
    with _lock:
        last = conn.execute("SELECT MAX(ts) FROM comments WHERE voter=?", (voter,)).fetchone()[0]
        if last is not None and now - last < COMMENT_GAP:
            raise TooSoon()
        conn.execute("INSERT INTO comments(ts, voter, text) VALUES(?,?,?)", (now, voter, text))
        conn.commit()
    return text


def all_comments(conn):
    with _lock:
        rows = conn.execute("SELECT id, ts, text FROM comments ORDER BY id").fetchall()
    return [{"id": i, "ts": round(ts, 1), "text": t} for i, ts, t in rows]


def current(conn, voter):
    with _lock:
        row = conn.execute("SELECT choice FROM votes WHERE voter=?", (voter,)).fetchone()
    return row[0] if row else None


def results(conn):
    with _lock:
        totals = dict(conn.execute("SELECT choice, COUNT(*) FROM votes GROUP BY choice"))
        events = conn.execute("SELECT ts, voter, choice FROM events ORDER BY id").fetchall()
        recent = conn.execute(
            "SELECT id, ts, text FROM comments ORDER BY id DESC LIMIT ?", (COMMENTS_SHOWN,)
        ).fetchall()
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
        "comments": [{"id": i, "ts": round(ts, 1), "text": t} for i, ts, t in reversed(recent)],
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
    if method == "GET" and path == "/ucl-logo.svg":
        return respond("200 OK", _read("ucl-logo.svg"), "image/svg+xml")
    if method == "GET" and path == "/api/me":
        return as_json({"choice": current(conn, voter)})
    if method == "GET" and path == "/api/comments":
        return as_json({"comments": all_comments(conn)})
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
    if method == "POST" and path == "/api/comment":
        try:
            length = min(int(environ.get("CONTENT_LENGTH") or 0), 4000)
            text = comment(conn, voter, json.loads(environ["wsgi.input"].read(length) or b"{}").get("text"))
        except TooSoon:
            return as_json({"error": f"one comment every {COMMENT_GAP} seconds, please"}, "429 Too Many Requests")
        except (ValueError, AttributeError):
            return as_json({"error": "text must be a non-empty string"}, "400 Bad Request")
        return as_json({"text": text})
    return as_json({"error": "not found"}, "404 Not Found")

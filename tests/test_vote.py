import io
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import vote  # noqa: E402


@pytest.fixture
def db(tmp_path):
    return str(tmp_path / "test.sqlite")


def call(db, method, path, body=None, cookie=None):
    data = json.dumps(body).encode() if body is not None else b""
    env = {
        "REQUEST_METHOD": method, "PATH_INFO": path, "SCRIPT_NAME": "/vote",
        "VOTE_DB": db, "CONTENT_LENGTH": str(len(data)), "wsgi.input": io.BytesIO(data),
        "wsgi.url_scheme": "https",
    }
    if cookie:
        env["HTTP_COOKIE"] = cookie
    seen = {}

    def start_response(status, headers):
        seen["status"], seen["headers"] = status, dict(headers)

    out = b"".join(vote.application(env, start_response))
    return seen["status"], seen["headers"], out


def phone(db):
    _, headers, _ = call(db, "GET", "/")
    return headers["Set-Cookie"].split(";")[0]


def totals(db):
    return json.loads(call(db, "GET", "/api/results")[2])


def test_pages_render(db):
    for path in ("/", "/results", "/qrcode.js"):
        status, headers, body = call(db, "GET", path)
        assert status == "200 OK"
        assert "X-Frame-Options" not in headers
    assert "liked James" in call(db, "GET", "/results")[2].decode()


def test_cookie_is_scoped_and_secure(db):
    cookie = call(db, "GET", "/")[1]["Set-Cookie"]
    assert "Path=/vote" in cookie and "Secure" in cookie and "HttpOnly" in cookie


def test_one_vote_per_phone_and_change_of_mind(db):
    a, b = phone(db), phone(db)
    call(db, "POST", "/api/vote", {"choice": "yes"}, a)
    call(db, "POST", "/api/vote", {"choice": "yes"}, a)
    call(db, "POST", "/api/vote", {"choice": "no"}, b)
    r = totals(db)
    assert (r["yes"], r["no"]) == (1, 1)
    call(db, "POST", "/api/vote", {"choice": "yes"}, b)
    r = totals(db)
    assert (r["yes"], r["no"]) == (2, 0)
    assert [p[1:] for p in r["timeline"]] == [[1, 0], [1, 1], [2, 0]]
    assert json.loads(call(db, "GET", "/api/me", cookie=b)[2]) == {"choice": "yes"}


def test_bad_votes_rejected(db):
    a = phone(db)
    assert call(db, "POST", "/api/vote", {"choice": "maybe"}, a)[0].startswith("400")
    assert call(db, "POST", "/api/vote", ["x"], a)[0].startswith("400")
    assert totals(db)["yes"] == 0


def test_timeline_is_capped(db):
    conn = vote.db(db)
    for i in range(500):
        vote.cast(conn, f"v{i:030d}", "yes" if i % 3 else "no", now=1000 + i)
    r = vote.results(conn)
    assert len(r["timeline"]) == vote.TIMELINE_POINTS
    assert r["timeline"][-1][1:] == [r["yes"], r["no"]]


def test_comments(db):
    a, b = phone(db), phone(db)
    assert call(db, "POST", "/api/comment", {"text": "  Great   talk  "}, a)[0] == "200 OK"
    assert call(db, "POST", "/api/comment", {"text": "again"}, a)[0].startswith("429")
    assert call(db, "POST", "/api/comment", {"text": "   "}, b)[0].startswith("400")
    assert call(db, "POST", "/api/comment", {"text": "x" * 500}, b)[0] == "200 OK"
    texts = [c["text"] for c in totals(db)["comments"]]
    assert texts == ["Great talk", "x" * vote.COMMENT_LENGTH]


def test_all_comments_exported_without_voter_ids(db):
    conn = vote.db(db)
    for i in range(vote.COMMENTS_SHOWN + 5):
        vote.comment(conn, f"v{i:030d}", f"comment {i}", now=1000 + i)
    assert len(totals(db)["comments"]) == vote.COMMENTS_SHOWN
    got = json.loads(call(db, "GET", "/api/comments")[2])["comments"]
    assert [c["text"] for c in got] == [f"comment {i}" for i in range(vote.COMMENTS_SHOWN + 5)]
    assert set(got[0]) == {"id", "ts", "text"}

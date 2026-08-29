"""The ask server, including the thread-safety of GET /plan.

A sqlite3 connection may only be used by the thread that created it. The
server handles each request on its own thread, so borrowing the agent's
connection raised ProgrammingError in production. These tests run the real
server over a real socket, so the handler thread is genuinely a different
thread from the one that built the database.
"""

import json
import sqlite3
import threading
import time
import urllib.error
import urllib.request

import pytest

import ask_server
import history


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "history.sqlite")
    conn = history.connect(path)
    history.record_plan(conn, "the plan text", {"gate_open": False}, ts=1787961600)
    conn.close()
    return path


@pytest.fixture
def server(cfg, db):
    """A live server whose plan provider uses whatever connection it is given."""
    def plan_provider(conn):
        plan = history.latest_plan(conn)
        return {"ts": plan["ts"], "text": plan["text"],
                "learning": {"open": False}, "thread": threading.current_thread().name}

    srv = ask_server.serve(cfg, lambda t, l: f"echo[{l}]: {t}", plan_provider,
                           host="127.0.0.1", port=0, db_path=db)
    assert srv is not None
    yield srv
    srv.shutdown()


def get(server, path):
    url = f"http://127.0.0.1:{server.server_address[1]}{path}"
    with urllib.request.urlopen(url, timeout=5) as r:
        return r.status, r.read().decode()


def post(server, path, payload):
    url = f"http://127.0.0.1:{server.server_address[1]}{path}"
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.status, r.read().decode()


# --- the bug ---------------------------------------------------------------

def test_plan_is_served_from_a_handler_thread(server):
    """The regression: this raised
    'SQLite objects created in a thread can only be used in that same thread'."""
    status, body = get(server, "/plan")
    assert status == 200
    payload = json.loads(body)
    assert payload["text"] == "the plan text"
    assert payload["thread"] != threading.main_thread().name, \
        "the handler must genuinely be on another thread for this to test anything"


def test_the_provider_gets_a_usable_connection(server):
    status, body = get(server, "/plan")
    assert status == 200 and json.loads(body)["ts"] == 1787961600


def test_repeated_requests_do_not_leak_or_break(server):
    for _ in range(15):
        assert get(server, "/plan")[0] == 200


def test_concurrent_requests_all_succeed(server):
    results, errors = [], []

    def hit():
        try:
            results.append(get(server, "/plan")[0])
        except Exception as e:                       # noqa: BLE001
            errors.append(repr(e))

    threads = [threading.Thread(target=hit) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert not errors, errors
    assert results == [200] * 8


def test_the_connection_handed_over_is_read_only(cfg, db):
    """A bug in the plan provider must not be able to corrupt history.

    The check runs inside the provider, on the handler's own thread: the
    connection belongs to that thread and is closed once the request ends.
    """
    def plan_provider(conn):
        try:
            conn.execute("CREATE TABLE nope (x INTEGER)")
            return {"writable": True}
        except sqlite3.OperationalError as e:
            return {"writable": False, "error": str(e)}

    srv = ask_server.serve(cfg, lambda t, l: "", plan_provider,
                           host="127.0.0.1", port=0, db_path=db)
    try:
        status, body = get(srv, "/plan")
    finally:
        srv.shutdown()
    assert status == 200
    assert json.loads(body)["writable"] is False


def test_readonly_connection_can_read_but_not_write(db):
    conn = history.readonly_connection(db)
    try:
        assert conn.execute("SELECT COUNT(*) c FROM plans").fetchone()["c"] == 1
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM plans")
    finally:
        conn.close()


def test_a_missing_database_is_reported_not_raised(cfg, tmp_path):
    srv = ask_server.serve(cfg, lambda t, l: "", lambda conn: {"x": 1},
                           host="127.0.0.1", port=0,
                           db_path=str(tmp_path / "absent.sqlite"))
    try:
        with pytest.raises(urllib.error.HTTPError) as e:
            get(srv, "/plan")
        assert e.value.code == 503
    finally:
        srv.shutdown()


def test_a_failing_provider_becomes_a_500(cfg, db):
    def boom(conn):
        raise RuntimeError("provider exploded")

    srv = ask_server.serve(cfg, lambda t, l: "", boom,
                           host="127.0.0.1", port=0, db_path=db)
    try:
        with pytest.raises(urllib.error.HTTPError) as e:
            get(srv, "/plan")
        assert e.value.code == 500
    finally:
        srv.shutdown()


# --- thread-local connections ----------------------------------------------

def test_each_thread_gets_its_own_connection(db):
    seen = {}

    def grab(name):
        seen[name] = history.thread_connection(db)

    grab("main")
    t = threading.Thread(target=grab, args=("worker",))
    t.start()
    t.join()
    assert seen["main"] is not seen["worker"]
    assert history.thread_connection(db) is seen["main"], "cached per thread"


def test_a_worker_thread_can_use_its_own_connection(db):
    out = {}

    def worker():
        try:
            conn = history.thread_connection(db)
            out["rows"] = conn.execute("SELECT COUNT(*) c FROM plans").fetchone()["c"]
        except Exception as e:                       # noqa: BLE001
            out["error"] = repr(e)

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    assert out.get("error") is None, out
    assert out["rows"] == 1


def test_resolve_accepts_a_connection_or_a_provider(db):
    conn = history.connect(db)
    try:
        assert history.resolve(conn) is conn, "a Connection is itself callable"
        assert history.resolve(lambda: conn) is conn
    finally:
        conn.close()


# --- the ask path still works ----------------------------------------------

def test_ask_still_answers(server):
    assert post(server, "/ask", {"text": "hola", "lang": "es"})[1] == "echo[es]: hola"


def test_unknown_paths_are_404(server):
    with pytest.raises(urllib.error.HTTPError) as e:
        get(server, "/nope")
    assert e.value.code == 404

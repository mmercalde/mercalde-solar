"""pi5/history_routes.py: the Pi5's /history proxy, and how app.py mounts it.

history_routes is imported on its own. app.py is only read as text: importing
it starts the Modbus poll and the generator loop.
"""

import gzip
import importlib.util
import json
import os
import re
import socket

import pytest

PI5 = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "pi5")
APP = os.path.join(PI5, "app.py")
NGINX = os.path.join(PI5, "..", "vps", "nginx", "default")

flask = pytest.importorskip("flask")


@pytest.fixture
def routes():
    spec = importlib.util.spec_from_file_location("history_routes",
                                                  os.path.join(PI5, "history_routes.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def client(routes):
    app = flask.Flask("t")
    app.register_blueprint(routes.history_bp)
    return app.test_client()


def closed_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class FakeResp:
    def __init__(self, status, body):
        self.status_code = status
        self.content = json.dumps(body).encode()


def test_kamrui_down_is_a_quick_503_not_an_exception(routes, client, monkeypatch):
    monkeypatch.setattr(routes, "HISTORY_API_URL", f"http://127.0.0.1:{closed_port()}/history/api/")
    r = client.get("/history/api/day?date=2026-11-07")
    assert r.status_code == 503 and r.get_json() == {"online": False}


def test_a_timeout_is_a_503_too(routes, client, monkeypatch):
    def slow(*a, **k):
        raise routes.requests.Timeout("slow")
    monkeypatch.setattr(routes.requests, "get", slow)
    assert client.get("/history/api/energy?from=2026-11-01&to=2026-11-07").status_code == 503


def test_only_known_names_and_args_are_forwarded(routes, client, monkeypatch):
    seen = []

    def fake(url, params=None, timeout=None):
        seen.append((url, params, timeout))
        return FakeResp(200, {"ok": True})
    monkeypatch.setattr(routes.requests, "get", fake)
    assert client.get("/history/api/samples").status_code == 404
    assert client.get("/history/api/day?date=2026-11-07&evil=1").status_code == 200
    assert seen == [(routes.HISTORY_API_URL + "day", {"date": "2026-11-07"},
                     routes.HISTORY_API_TIMEOUT)]
    assert max(routes.HISTORY_API_TIMEOUT) <= 5 and routes.HISTORY_API_TIMEOUT[0] <= 2


def test_upstream_400_passes_through_and_500_reads_as_offline(routes, client, monkeypatch):
    monkeypatch.setattr(routes.requests, "get",
                        lambda *a, **k: FakeResp(400, {"error": "date must be YYYY-MM-DD"}))
    r = client.get("/history/api/day?date=x")
    assert r.status_code == 400 and "YYYY" in r.get_json()["error"]
    monkeypatch.setattr(routes.requests, "get", lambda *a, **k: FakeResp(503, {}))
    assert client.get("/history/api/day?date=2026-11-07").get_json()["online"] is False


def test_big_answers_are_gzipped_when_asked(routes, client, monkeypatch):
    body = {"series": {"t": list(range(1440))}}
    monkeypatch.setattr(routes.requests, "get", lambda *a, **k: FakeResp(200, body))
    r = client.get("/history/api/day?date=2026-11-07", headers={"Accept-Encoding": "gzip"})
    assert r.headers["Content-Encoding"] == "gzip"
    assert json.loads(gzip.decompress(r.data)) == body
    r = client.get("/history/api/day?date=2026-11-07")
    assert "Content-Encoding" not in r.headers and r.get_json() == body


def test_chart_library_is_served_locally(client):
    for name in ("uPlot.iife.min.js", "uPlot.min.css", "history.js", "history.css"):
        r = client.get(f"/history/static/{name}")
        assert r.status_code == 200 and len(r.data) > 500, name
        r.close()


def test_history_js_reaches_nothing_off_the_pi5():
    src = open(os.path.join(PI5, "static", "history.js")).read()
    assert not re.search(r"https?://", src.split("*/", 1)[1])
    assert "America/Tijuana" in src and "hour12: true" in src


def test_app_mounts_the_blueprint_and_loads_only_local_files():
    src = open(APP).read()
    assert "from history_routes import history_bp" in src
    assert "app.register_blueprint(history_bp)" in src
    section = src[src.index("<details class='secacc' id='histSec'>"):]
    section = section[:section.index("</script>")]
    assert "/history/static/uPlot.iife.min.js" in section and "http" not in section


def test_dashboard_ids_are_unique():
    """A repeated id is how "Peak -- kW" stayed blank: getElementById finds the first."""
    src = open(APP).read()
    start = src.index('DASHBOARD_HTML = """')
    html = src[start:src.index('"""', start + 20)]
    ids = re.findall(r"\bid='([^']+)'", html)
    dup = sorted({i for i in ids if ids.count(i) > 1})
    assert dup == []


def test_nginx_proxies_the_history_prefix_to_the_pi5():
    conf = open(NGINX).read()
    block = conf[conf.index("location /history/"):]
    block = block[:block.index("}")]
    assert "proxy_pass http://solar_dashboard;" in block
    assert "solar_locked" not in block


def test_three_js_is_committed_and_served_before_the_cdn():
    path = os.path.join(PI5, "static", "three.min.js")
    assert os.path.getsize(path) > 500_000
    assert '"128"' in open(path).read()          # r128, the revision the scene was built on
    src = open(APP).read()
    route = src[src.index("def vendor_file"):].split("\n\n")[0]
    assert "'static', name" in route
    js = src[src.index("libSrc(['/vendor/three.min.js'"):][:200]
    assert js.index("/vendor/three.min.js") < js.index("cdnjs")


def test_charge_label_and_failed_battery_reads():
    src = open(APP).read()
    assert "AC charge" not in src and "DC charge power" in src
    assert 'new_data["batteryVoltageOk"] = val is not None' in src
    assert 'new_data["batterySocOk"] = val is not None' in src
    # the values the rest of the program reads are unchanged
    assert 'new_data["batteryVoltage"] = round(val / 1000.0, 2) if val is not None else 0.0' in src
    assert 'new_data["batterySOC"] = val if val is not None else 0' in src
    assert "setNumOr('batterySOC_value',socOk,soc)" in src

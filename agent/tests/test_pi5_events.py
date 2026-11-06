"""GET /events, as pi5/app.py ships it. The VPS has asked for this route for as
long as the record goes back and it did not exist, so Alexa's "what happened" has
always failed. Loaded the way test_ags_reasons.py does, from the real file."""

import ast
import os
import re

APP = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "pi5", "app.py")
VPS = os.path.join(os.path.dirname(APP), "..", "vps", "alexa_solar.py")


def load(*names):
    tree = ast.parse(open(APP).read())
    keep = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    ns = {}
    exec(compile(ast.Module(body=keep, type_ignores=[]), APP, "exec"), ns)
    return ns


def test_events_come_newest_first_without_touching_the_log():
    state = {"events": ["09:00:00 - old", "09:05:00 - new"]}
    assert load("_events_newest_first")["_events_newest_first"](state) == \
        ["09:05:00 - new", "09:00:00 - old"]
    assert state["events"] == ["09:00:00 - old", "09:05:00 - new"]


def test_the_route_is_the_one_the_vps_calls_and_is_read_only():
    source = open(APP).read()
    assert re.search(r'ESP8266_EVENTS_URL = "http://10\.8\.0\.2:8080/events"', open(VPS).read())
    assert "@app.route('/events')\n" in source
    route = source[source.index("@app.route('/events')"):].split("\n\n")[0]
    assert '"events"' in route
    assert "methods" not in route and "write_" not in route

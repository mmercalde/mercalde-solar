"""History section: static files and a read-only proxy to the KAMRUI.

Registered by app.py as a blueprint. Everything is under /history so the VPS
needs one nginx location:

  /history/static/<file>      uPlot and the History section's JS/CSS, from pi5/static.
                              Self-hosted: the page must chart on the LAN with no uplink.
  /history/api/<name>         GET only, forwarded to history_api.py on the KAMRUI.

The proxy never raises and never blocks long: the dashboard and the generator
loop share this process, and KAMRUI being down must cost them nothing. It is
a separate module so it can be tested without importing app.py, whose import
starts the Modbus poll.
"""

import gzip
import logging
import os

import requests
from flask import Blueprint, Response, jsonify, request

log = logging.getLogger(__name__)

HISTORY_API_URL = "http://192.168.3.152:8091/history/api/"
# (connect, read). gunicorn here has four threads; a KAMRUI that is off the
# network must not hold one for long, so the connect half is short.
HISTORY_API_TIMEOUT = (2, 5)
ALLOWED = ("day", "energy", "health")
ALLOWED_ARGS = ("date", "from", "to")
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

history_bp = Blueprint("history", __name__, url_prefix="/history",
                       static_folder=STATIC_DIR, static_url_path="/static")


@history_bp.route("/api/<name>")
def history_api(name):
    if name not in ALLOWED:
        return jsonify({"error": "not found"}), 404
    params = {k: request.args[k] for k in ALLOWED_ARGS if k in request.args}
    try:
        r = requests.get(HISTORY_API_URL + name, params=params,
                         timeout=HISTORY_API_TIMEOUT)
    except requests.RequestException as e:
        log.debug("history API unavailable: %s", e)
        return jsonify({"online": False}), 503
    if r.status_code >= 500:
        return jsonify({"online": False, "status": r.status_code}), 503
    body = r.content
    headers = {"Cache-Control": "no-store"}
    # A day is ~120 KB of JSON and gzips to about a fifth; it crosses the VPS.
    if len(body) > 2048 and "gzip" in request.headers.get("Accept-Encoding", ""):
        body = gzip.compress(body, 5)
        headers["Content-Encoding"] = "gzip"
        headers["Vary"] = "Accept-Encoding"
    return Response(body, status=r.status_code, headers=headers,
                    mimetype="application/json")

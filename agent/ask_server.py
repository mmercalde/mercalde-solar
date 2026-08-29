"""Small HTTP front end for Alexa (SPEC section 8).

  POST /ask   {"text": "...", "lang": "en"|"es"}  -> plain text, 60 words max
  GET  /plan                                       -> the latest plan record as JSON

Bound to the KAMRUI LAN address only, never 0.0.0.0: the VPS reaches it over
WireGuard through the Pi5, and nothing else should.
"""

import json
import logging
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import history

log = logging.getLogger(__name__)

BIND_HOST = "192.168.3.152"
MAX_BODY = 8192
FALLBACK = {"en": "The solar agent is not answering right now.",
            "es": "El agente solar no responde en este momento."}


class _Handler(BaseHTTPRequestHandler):
    server_version = "solar-agent"
    ask_handler = None      # set on the server instance
    plan_provider = None

    def log_message(self, fmt, *args):
        log.debug("ask_server %s - %s", self.address_string(), fmt % args)

    def _send(self, code, body, content_type="text/plain; charset=utf-8"):
        payload = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        if self.path.rstrip("/") != "/plan":
            self._send(404, "not found")
            return
        if not self.server.plan_provider:
            self._send(503, json.dumps({"error": "no plan provider"}),
                       "application/json")
            return
        # Each request runs on its own thread, and a sqlite3 connection may
        # only be used by the thread that created it. Open a short-lived
        # read-only connection here rather than borrowing the agent's.
        conn = None
        try:
            conn = history.readonly_connection(self.server.db_path)
            plan = self.server.plan_provider(conn)
        except sqlite3.Error as e:
            log.warning("could not read the plan: %s", e)
            self._send(503, json.dumps({"error": f"database unavailable: {e}"}),
                       "application/json")
            return
        except Exception:                            # noqa: BLE001
            log.exception("plan provider failed")
            self._send(500, json.dumps({"error": "plan provider failed"}),
                       "application/json")
            return
        finally:
            if conn is not None:
                conn.close()
        self._send(200, json.dumps(plan, default=str), "application/json")

    def do_POST(self):
        if self.path.rstrip("/") != "/ask":
            self._send(404, "not found")
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY:
            self._send(400, "bad request")
            return
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            text = (body.get("text") or "").strip()
            lang = body.get("lang") if body.get("lang") in ("en", "es") else "en"
        except (ValueError, UnicodeDecodeError):
            self._send(400, "bad request")
            return
        if not text:
            self._send(400, "bad request")
            return

        try:
            reply = self.server.ask_handler(text, lang)
        except Exception:                            # noqa: BLE001
            # Alexa is waiting with an 8 s timeout; always say something.
            log.exception("ask handler failed")
            reply = FALLBACK[lang]
        self._send(200, reply or FALLBACK[lang])


class AskServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, ask_handler, plan_provider, db_path=None):
        super().__init__(addr, _Handler)
        self.ask_handler = ask_handler
        self.plan_provider = plan_provider
        self.db_path = db_path


def serve(cfg, ask_handler, plan_provider, host=None, port=None, db_path=None):
    """Start the server on a daemon thread. Returns the server, or None.

    `plan_provider` is called with a read-only connection opened for that
    request; see _Handler.do_GET.
    """
    host = host or BIND_HOST
    # `or` would swallow port 0, which asks the OS for a free port.
    port = cfg["ask_port"] if port is None else port
    try:
        server = AskServer((host, port), ask_handler, plan_provider, db_path)
    except OSError as e:
        log.error("cannot bind ask server to %s:%s (%s). Alexa and /plan will "
                  "be unavailable; the rest of the agent runs normally.",
                  host, port, e)
        return None
    threading.Thread(target=server.serve_forever, name="ask-server",
                     daemon=True).start()
    log.info("ask server listening on %s:%s", host, port)
    return server

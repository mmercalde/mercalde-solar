"""Small HTTP front end for Alexa (SPEC section 8).

  POST /ask   {"text": "...", "lang": "en"|"es"}  -> plain text, 60 words max
              add "deliver": "telegram", "wait_s": N  -> the same 200 if the answer
              comes within N seconds, otherwise 202, and the answer goes to Telegram
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
import telegram

log = logging.getLogger(__name__)

BIND_HOST = "192.168.3.152"
MAX_BODY = 8192
FALLBACK = {"en": "The solar agent is not answering right now.",
            "es": "El agente solar no responde en este momento."}
LATER = {"en": "The answer will be sent by Telegram.",
         "es": "La respuesta se enviará por Telegram."}
MAX_WAIT_S = 30


class _Delivery:
    """One question answered on its own thread: to the caller, or else to Telegram.

    Gemma takes 15 to 30 s and Alexa gives up after about 8. Whichever of the
    answer and the caller's deadline comes first decides, under the lock, so an
    answer landing on the deadline goes exactly one way.
    """

    def __init__(self, cfg, ask_handler, text, lang):
        self.cfg, self.ask_handler, self.text, self.lang = cfg, ask_handler, text, lang
        self.lock = threading.Lock()
        self.done = threading.Event()
        self.reply = None
        self.gave_up = False
        threading.Thread(target=self._run, name="ask-delivery", daemon=True).start()

    def _run(self):
        # Through the normal handler, so it queues behind the agent's lock.
        try:
            reply = self.ask_handler(self.text, self.lang) or FALLBACK[self.lang]
        except Exception:                            # noqa: BLE001
            log.exception("ask handler failed in the background")
            reply = FALLBACK[self.lang]
        with self.lock:
            self.reply = reply
            late = self.gave_up
            self.done.set()
        if late:
            self._send(reply)

    def _send(self, reply):
        try:
            sent = telegram.send(self.cfg, telegram.escape(f"🎙 {self.text}\n{reply}"))
        except Exception:                            # noqa: BLE001
            log.exception("could not send a late answer to Telegram")
            return
        if not sent:
            log.warning("a late answer did not reach Telegram: %s", self.text[:80])

    def wait(self, seconds):
        """The answer if it came in time. Otherwise None, and Telegram gets it."""
        self.done.wait(seconds)
        with self.lock:
            if self.reply is None:
                self.gave_up = True
            return self.reply


def _wait_seconds(value):
    try:
        return max(0.0, min(float(value), MAX_WAIT_S))
    except (TypeError, ValueError):
        return 0.0


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

        if body.get("deliver") == "telegram":
            reply = _Delivery(self.server.cfg, self.server.ask_handler, text,
                              lang).wait(_wait_seconds(body.get("wait_s")))
            if reply is None:
                self._send(202, LATER[lang])
            else:
                self._send(200, reply)
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

    def __init__(self, addr, ask_handler, plan_provider, db_path=None, cfg=None):
        super().__init__(addr, _Handler)
        self.cfg = cfg
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
        server = AskServer((host, port), ask_handler, plan_provider, db_path, cfg)
    except OSError as e:
        log.error("cannot bind ask server to %s:%s (%s). Alexa and /plan will "
                  "be unavailable; the rest of the agent runs normally.",
                  host, port, e)
        return None
    threading.Thread(target=server.serve_forever, name="ask-server",
                     daemon=True).start()
    log.info("ask server listening on %s:%s", host, port)
    return server

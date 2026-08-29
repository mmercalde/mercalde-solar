"""Telegram bot I/O.

Same bot token and chat id the dashboard uses (SPEC section 2). The dashboard
only sends; the agent is the sole consumer of getUpdates, so the long poll here
is safe to run continuously.
"""

import logging

import requests

log = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/{method}"
MAX_MESSAGE = 4096


def _url(cfg, method):
    return API.format(token=cfg["telegram"]["token"], method=method)


def configured(cfg):
    tg = cfg.get("telegram") or {}
    return bool(tg.get("token") and tg.get("chat_id"))


def send(cfg, text, timeout=15):
    """Send a message to the configured chat. Returns True on success."""
    if not configured(cfg):
        log.warning("telegram not configured; dropping message: %s", text[:80])
        return False
    try:
        r = requests.post(_url(cfg, "sendMessage"), timeout=timeout, data={
            "chat_id": cfg["telegram"]["chat_id"],
            "text": text[:MAX_MESSAGE],
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        })
        body = r.json()
    except (requests.RequestException, ValueError) as e:
        log.warning("telegram send failed: %s", e)
        return False
    if not body.get("ok"):
        log.warning("telegram refused the message: %s", body.get("description"))
        return False
    return True


def get_updates(cfg, offset=None, poll_seconds=50, timeout=70):
    """Long-poll for inbound messages.

    Yields (update_id, text) for messages from the configured chat only;
    anything from another chat is logged and dropped (SPEC section 8).
    """
    if not configured(cfg):
        return []
    params = {"timeout": poll_seconds, "allowed_updates": '["message"]'}
    if offset is not None:
        params["offset"] = offset
    try:
        r = requests.get(_url(cfg, "getUpdates"), params=params, timeout=timeout)
        body = r.json()
    except (requests.RequestException, ValueError) as e:
        log.warning("telegram getUpdates failed: %s", e)
        return []
    if not body.get("ok"):
        log.warning("telegram getUpdates refused: %s", body.get("description"))
        return []

    wanted = str(cfg["telegram"]["chat_id"])
    out = []
    for upd in body.get("result", []):
        msg = upd.get("message") or {}
        chat_id = str((msg.get("chat") or {}).get("id", ""))
        text = (msg.get("text") or "").strip()
        if chat_id != wanted:
            log.warning("ignoring message from unauthorised chat %s", chat_id)
            out.append((upd["update_id"], None))
            continue
        out.append((upd["update_id"], text or None))
    return out

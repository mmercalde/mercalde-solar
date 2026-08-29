"""Telegram bot I/O.

Same bot token and chat id the dashboard uses (SPEC section 2). The dashboard
only sends; the agent is the sole consumer of getUpdates, so the long poll here
is safe to run continuously.
"""

import html
import logging
import re

import requests

log = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/{method}"
MAX_MESSAGE = 4096


def escape(text):
    """Make arbitrary text safe inside an HTML-parsed message.

    Everything the agent sends goes out with parse_mode=HTML so the digests
    can use <b>. A plan record says "peak 56.0 < 57.0", Telegram read the
    "< 5" as the start of a tag, and refused the whole message with "can't
    parse entities: Unsupported start tag" - which is why the action messages
    of the first live days never arrived. Model output, reasons and plan
    records are text, not markup, and are escaped here before being placed in
    a message the agent marks up itself.
    """
    return html.escape(str(text), quote=False)


def _url(cfg, method):
    return API.format(token=cfg["telegram"]["token"], method=method)


def configured(cfg):
    tg = cfg.get("telegram") or {}
    return bool(tg.get("token") and tg.get("chat_id"))


def _post(cfg, text, timeout, parse_mode=None):
    """One sendMessage attempt. Returns (ok, description)."""
    data = {"chat_id": cfg["telegram"]["chat_id"],
            "text": text[:MAX_MESSAGE],
            "disable_web_page_preview": True}
    if parse_mode:
        data["parse_mode"] = parse_mode
    try:
        body = requests.post(_url(cfg, "sendMessage"), timeout=timeout,
                             data=data).json()
    except (requests.RequestException, ValueError) as e:
        return False, str(e)
    return bool(body.get("ok")), body.get("description")


def send(cfg, text, timeout=15):
    """Send a message to the configured chat. Returns True on success.

    A message refused for its markup is resent once as plain text. An action
    the owner is not told about is worse than one told without its bold, and
    a silent drop is how the first live days lost every action message.
    """
    if not configured(cfg):
        log.warning("telegram not configured; dropping message: %s", text[:80])
        return False
    ok, why = _post(cfg, text, timeout, parse_mode="HTML")
    if ok:
        return True
    log.warning("telegram refused the message (%s); resending as plain text", why)
    ok, why = _post(cfg, _strip_tags(text), timeout)
    if not ok:
        log.warning("telegram refused the plain-text resend too: %s", why)
    return ok


def _strip_tags(text):
    """The message as words, for the plain-text resend."""
    out = re.sub(r"</?(?:b|i|u|s|code|pre|a|tg-spoiler|blockquote)\b[^>]*>", "", text)
    return html.unescape(out)


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

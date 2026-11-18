"""Outbound webhook integrations: notify external tools of newsletter events."""
import hashlib
import hmac
import json
import logging

from . import db

logger = logging.getLogger(__name__)


def dispatch(conn, event_name: str, payload: dict) -> int:
    import httpx

    rows = conn.execute("SELECT * FROM webhooks WHERE active = 1").fetchall()
    delivered = 0
    for row in rows:
        subscribed = json.loads(row["events"] or "[]")
        if subscribed and event_name not in subscribed:
            continue
        body = {"event": event_name, "data": payload}
        body_bytes = json.dumps(body).encode()
        headers = {"Content-Type": "application/json", "X-Newsletter-Event": event_name}
        if row["secret"]:
            headers["X-Newsletter-Signature"] = hmac.new(
                row["secret"].encode(), body_bytes, hashlib.sha256
            ).hexdigest()
        try:
            resp = httpx.post(row["url"], content=body_bytes, headers=headers, timeout=10)
            delivered += 1 if resp.status_code < 300 else 0
        except httpx.HTTPError as exc:
            logger.warning("Webhook %s failed: %s", row["name"], exc)
    return delivered


def dispatch_async(event_name: str, payload: dict) -> None:
    """Fire-and-forget dispatch in a short background thread."""
    import threading

    threading.Thread(target=_run, args=(event_name, payload), daemon=True).start()


def _run(event_name: str, payload: dict) -> None:
    try:
        with db.get_db() as conn:
            dispatch(conn, event_name, payload)
    except Exception as exc:
        logger.warning("Async webhook dispatch failed: %s", exc)

"""Subscriber event tracking (behavioural data used by segments, automations,
analytics and referrals)."""
import json

from . import db


def record_event(conn, contact_id: str | None, ev_type: str,
                 email: str | None = None, source: str | None = None,
                 value: str | None = None) -> None:
    conn.execute(
        """INSERT INTO subscriber_events (id, contact_id, email, type, source, value, timestamp)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (db.new_id(), contact_id, email, ev_type, source, value, db.now()),
    )


def record_event_now(contact_id: str | None, ev_type: str,
                     email: str | None = None, source: str | None = None,
                     value: str | None = None) -> None:
    with db.get_db() as conn:
        record_event(conn, contact_id, ev_type, email, source, value)


def contact_event_types(conn, contact_id: str) -> set:
    return {
        r["type"]
        for r in conn.execute(
            "SELECT DISTINCT type FROM subscriber_events WHERE contact_id = ? OR email ="
            " (SELECT email FROM contacts WHERE id = ?)",
            (contact_id, contact_id),
        )
    }


def contact_tags(conn, contact_id: str) -> set:
    return {
        r["name"]
        for r in conn.execute(
            "SELECT t.name FROM tags t JOIN contact_tags ct ON ct.tag_id = t.id"
            " WHERE ct.contact_id = ?",
            (contact_id,),
        )
    }


def events_json_to_text(value) -> str:
    if value is None:
        return ""
    return value if isinstance(value, str) else json.dumps(value)

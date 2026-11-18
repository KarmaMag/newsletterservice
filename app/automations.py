"""Automation engine: multi-step email sequences with triggers
(welcome on signup, tag added, manual enroll) and scheduled step delivery.

Processing is stepped by ``tick()`` — call it from the UI, the agent, or cron:
    POST /api/automations/tick
"""
import logging
import os

from . import db, mailer
from .events import record_event, contact_event_types

logger = logging.getLogger(__name__)

TRIGGER_TYPES = ("manual", "signup", "tag_added", "purchased")


def create_sequence(conn, name: str, audience_id: str | None = None,
                    trigger_type: str = "manual", trigger_value: str | None = None) -> dict:
    sequence_id = db.new_id()
    conn.execute(
        """INSERT INTO sequences (id, name, audience_id, trigger_type, trigger_value, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (sequence_id, name, audience_id, trigger_type, trigger_value, db.now()),
    )
    return _get_sequence(conn, sequence_id)


def _get_sequence(conn, sequence_id: str) -> dict:
    row = conn.execute("SELECT * FROM sequences WHERE id = ?", (sequence_id,)).fetchone()
    sequence = db.row_to_dict(row)
    sequence["steps"] = [
        db.row_to_dict(r)
        for r in conn.execute(
            "SELECT * FROM sequence_steps WHERE sequence_id = ? ORDER BY position",
            (sequence_id,),
        )
    ]
    return sequence


def add_step(conn, sequence_id: str, position: int, subject: str, html: str,
             delay_hours: float = 0.0) -> dict:
    sequence_id_existing = conn.execute(
        "SELECT id FROM sequences WHERE id = ?", (sequence_id,)
    ).fetchone()
    if not sequence_id_existing:
        raise ValueError(f"Sequence {sequence_id} not found")
    step_id = db.new_id()
    conn.execute(
        """INSERT INTO sequence_steps
             (id, sequence_id, position, delay_hours, subject, html_content)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (step_id, sequence_id, position, delay_hours, subject, html),
    )
    row = conn.execute("SELECT * FROM sequence_steps WHERE id = ?", (step_id,)).fetchone()
    return db.row_to_dict(row)


def enroll(conn, sequence_id: str, contact_id: str) -> dict | None:
    sequence = conn.execute("SELECT * FROM sequences WHERE id = ?", (sequence_id,)).fetchone()
    contact = conn.execute("SELECT * FROM contacts WHERE id = ?", (contact_id,)).fetchone()
    if not sequence or not contact:
        return None
    if sequence["status"] != "active":
        return None
    existing = conn.execute(
        "SELECT id FROM sequence_enrollments WHERE sequence_id = ? AND contact_id = ?",
        (sequence_id, contact_id),
    ).fetchone()
    if existing:
        return None
    steps = conn.execute(
        "SELECT * FROM sequence_steps WHERE sequence_id = ? ORDER BY position LIMIT 1",
        (sequence_id,),
    ).fetchone()
    first_delay = (steps["delay_hours"] if steps else 24) * 3600
    conn.execute(
        """INSERT INTO sequence_enrollments
             (id, sequence_id, contact_id, next_step, next_send_at, status, created_at)
           VALUES (?, ?, ?, 0, ?, 'active', ?)""",
        (db.new_id(), sequence_id, contact_id, db.now() + first_delay, db.now()),
    )
    record_event(conn, contact_id, "sequence_enrolled", value=sequence_id)
    return {"enrolled": True, "sequence_id": sequence_id, "contact_id": contact_id}


def enroll_by_trigger(conn, trigger_type: str, trigger_value: str | None,
                      contact_id: str, audience_id: str | None = None) -> None:
    """Fire a behavioural trigger: enroll the contact into matching active sequences."""
    rows = conn.execute(
        """SELECT id FROM sequences
           WHERE status = 'active' AND trigger_type = ?
             AND (trigger_value IS NULL OR trigger_value = '' OR trigger_value = ?)
             AND (audience_id IS NULL OR audience_id = ?)""",
        (trigger_type, trigger_value, audience_id),
    ).fetchall()
    for row in rows:
        enroll(conn, row["id"], contact_id)


def _send_step(step: dict, contact: dict) -> bool:
    if not mailer.is_configured():
        logger.info("SMTP not configured; step %s to %s recorded without send",
                    step["id"], contact["email"])
        return True
    from_name = mailer.smtp_config()["from_name"]
    from_email = mailer.smtp_config()["from_email"]
    unsub = f"{_public_base()}/unsubscribe?email={contact['email']}"
    try:
        mailer.send_email(
            contact["email"], step["subject"], step["html_content"],
            unsubscribe_url=unsub,
        )
    except Exception as exc:
        logger.warning("Sequence step to %s failed: %s", contact["email"], exc)
        return False
    return True


def _public_base() -> str:
    return os.environ.get("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")


def tick(conn = None, limit: int = 100) -> dict:
    """Send all due sequence steps. Safe to call repeatedly (idempotent)."""
    sends = 0
    completed = 0
    with db.get_db() as own_conn:
        conn = conn or own_conn
        due = conn.execute(
            """SELECT e.id AS enrollment_id, e.contact_id, s.id AS sequence_id
               FROM sequence_enrollments e
               JOIN sequences s ON s.id = e.sequence_id
               WHERE e.status = 'active' AND e.next_send_at IS NOT NULL
                 AND e.next_send_at <= ?
               ORDER BY e.next_send_at LIMIT ?""",
            (db.now(), limit),
        ).fetchall()
        for row in due:
            result = _process_enrollment(conn, row["sequence_id"], row["contact_id"])
            if result == "sent":
                sends += 1
            elif result == "completed":
                completed += 1
    return {"sends": sends, "completed": completed}


def _process_enrollment(conn, sequence_id: str, contact_id: str) -> str:
    enrollment = conn.execute(
        "SELECT * FROM sequence_enrollments WHERE sequence_id = ? AND contact_id = ?",
        (sequence_id, contact_id),
    ).fetchone()
    contact = conn.execute("SELECT * FROM contacts WHERE id = ?", (contact_id,)).fetchone()
    if not enrollment or not contact:
        return "missing"
    if contact["status"] == "unsubscribed":
        conn.execute("UPDATE sequence_enrollments SET status = 'stopped' WHERE id = ?",
                     (enrollment["id"],))
        return "stopped"
    step = conn.execute(
        "SELECT * FROM sequence_steps WHERE sequence_id = ? AND position = ?",
        (sequence_id, enrollment["next_step"]),
    ).fetchone()
    if not step:
        conn.execute(
            "UPDATE sequence_enrollments SET status = 'completed', next_send_at = NULL WHERE id = ?",
            (enrollment["id"],),
        )
        record_event(conn, contact_id, "sequence_completed", value=sequence_id)
        return "completed"
    sent = _send_step(step, dict(contact))
    if not sent:
        return "failed"
    record_event(conn, contact_id, "sequence_email_sent", value=step["id"])
    next_step = enrollment["next_step"] + 1
    following = conn.execute(
        "SELECT delay_hours FROM sequence_steps WHERE sequence_id = ? AND position = ?",
        (sequence_id, next_step),
    ).fetchone()
    if following:
        next_at = db.now() + following["delay_hours"] * 3600
        conn.execute(
            "UPDATE sequence_enrollments SET next_step = ?, next_send_at = ? WHERE id = ?",
            (next_step, next_at, enrollment["id"]),
        )
    else:
        conn.execute(
            "UPDATE sequence_enrollments SET next_step = ?, next_send_at = NULL,"
            " status = 'completed' WHERE id = ?",
            (next_step, enrollment["id"]),
        )
        record_event(conn, contact_id, "sequence_completed", value=sequence_id)
        return "completed"
    return "sent"

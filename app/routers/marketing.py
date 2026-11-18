"""Marketing router: templates, automated sequences, segments, A/B tests."""
import json
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from .. import ab, automations, db, mailer
from ..events import record_event

router = APIRouter(prefix="/api")


# ---------- models ------------------------------------------------------------

class TemplateIn(BaseModel):
    name: str
    subject: str = ""
    html_content: str


class SequenceIn(BaseModel):
    name: str
    audience_id: Optional[str] = None
    trigger_type: str = "manual"
    trigger_value: Optional[str] = None


class StepIn(BaseModel):
    subject: str
    html_content: str
    position: int
    delay_hours: float = 0.0


class EnrollIn(BaseModel):
    contact_id: Optional[str] = None
    segment_id: Optional[str] = None
    tag: Optional[str] = None


class SegmentIn(BaseModel):
    name: str
    rules: list[dict] = Field(default_factory=list)


class AbTestIn(BaseModel):
    name: str
    variants: list[dict]


# ---------- helpers ------------------------------------------------------------

def _or_404(row, what: str) -> dict:
    if not row:
        raise HTTPException(404, f"{what} not found")
    return db.row_to_dict(row)


def _contacts_from_enroll(conn, payload: EnrollIn) -> list[dict]:
    from ..segments import evaluate

    if payload.contact_id:
        row = conn.execute("SELECT * FROM contacts WHERE id = ?", (payload.contact_id,)).fetchone()
        return [_or_404(row, "Contact")]
    if payload.segment_id:
        segment = _or_404(conn.execute("SELECT * FROM segments WHERE id = ?", (payload.segment_id,)).fetchone(), "Segment")
        return evaluate(segment["rules"])
    if payload.tag:
        return evaluate([{"field": "tag", "op": "any", "value": payload.tag}])
    raise HTTPException(400, "Provide contact_id, segment_id or tag")


# ---------- templates ----------------------------------------------------------

@router.get("/templates")
def list_templates():
    with db.get_db() as conn:
        rows = conn.execute("SELECT * FROM templates ORDER BY created_at DESC").fetchall()
        return [db.row_to_dict(r) for r in rows]


@router.post("/templates", status_code=201)
def create_template(payload: TemplateIn):
    with db.get_db() as conn:
        template_id = db.new_id()
        conn.execute(
            "INSERT INTO templates (id, name, subject, html_content, created_at) VALUES (?, ?, ?, ?, ?)",
            (template_id, payload.name, payload.subject, payload.html_content, db.now()),
        )
        return _or_404(conn.execute("SELECT * FROM templates WHERE id = ?", (template_id,)).fetchone(), "Template")


@router.delete("/templates/{template_id}", status_code=204)
def delete_template(template_id: str):
    with db.get_db() as conn:
        if conn.execute("DELETE FROM templates WHERE id = ?", (template_id,)).rowcount == 0:
            raise HTTPException(404, "Template not found")


# ---------- automations / sequences --------------------------------------------

@router.get("/automations/sequences")
def list_sequences():
    with db.get_db() as conn:
        rows = conn.execute("SELECT * FROM sequences ORDER BY created_at DESC").fetchall()
        sequences = []
        for r in rows:
            sequence = db.row_to_dict(r)
            sequence["steps"] = [
                db.row_to_dict(s)
                for s in conn.execute(
                    "SELECT * FROM sequence_steps WHERE sequence_id = ? ORDER BY position",
                    (r["id"],),
                )
            ]
            sequence["enrollments"] = conn.execute(
                "SELECT COUNT(*) c FROM sequence_enrollments WHERE sequence_id = ?",
                (r["id"],),
            ).fetchone()["c"]
            sequence["completed"] = conn.execute(
                "SELECT COUNT(*) c FROM sequence_enrollments WHERE sequence_id = ? AND status = 'completed'",
                (r["id"],),
            ).fetchone()["c"]
            sequences.append(sequence)
        return sequences


@router.post("/automations/sequences", status_code=201)
def create_sequence(payload: SequenceIn):
    if payload.trigger_type not in automations.TRIGGER_TYPES:
        raise HTTPException(400, f"trigger_type must be one of {automations.TRIGGER_TYPES}")
    with db.get_db() as conn:
        return automations.create_sequence(
            conn, payload.name, payload.audience_id,
            payload.trigger_type, payload.trigger_value,
        )


@router.post("/automations/sequences/{sequence_id}/steps", status_code=201)
def add_sequence_step(sequence_id: str, payload: StepIn):
    with db.get_db() as conn:
        try:
            return automations.add_step(
                conn, sequence_id, payload.position, payload.subject,
                payload.html_content, payload.delay_hours,
            )
        except ValueError as exc:
            raise HTTPException(404, str(exc))


@router.patch("/automations/sequences/{sequence_id}")
def update_sequence(sequence_id: str, body: dict):
    with db.get_db() as conn:
        _or_404(conn.execute("SELECT id FROM sequences WHERE id = ?", (sequence_id,)).fetchone(), "Sequence")
        for key in ("status", "name", "trigger_type", "trigger_value"):
            if key in body:
                conn.execute(f"UPDATE sequences SET {key} = ? WHERE id = ?", (body[key], sequence_id))
        return _or_404(conn.execute("SELECT * FROM sequences WHERE id = ?", (sequence_id,)).fetchone(), "Sequence")


@router.delete("/automations/sequences/{sequence_id}", status_code=204)
def delete_sequence(sequence_id: str):
    with db.get_db() as conn:
        conn.execute("DELETE FROM sequence_enrollments WHERE sequence_id = ?", (sequence_id,))
        conn.execute("DELETE FROM sequence_steps WHERE sequence_id = ?", (sequence_id,))
        if conn.execute("DELETE FROM sequences WHERE id = ?", (sequence_id,)).rowcount == 0:
            raise HTTPException(404, "Sequence not found")


@router.post("/automations/sequences/{sequence_id}/enroll")
def enroll_contacts(sequence_id: str, payload: EnrollIn):
    with db.get_db() as conn:
        contacts = _contacts_from_enroll(conn, payload)
        enrolled = sum(1 for c in contacts if automations.enroll(conn, sequence_id, c["id"]))
    return {"enrolled": enrolled, "eligible": len(contacts)}


@router.post("/automations/tick")
def tick_automations():
    return automations.tick()


@router.get("/automations/enrollments")
def list_enrollments(status: Optional[str] = Query(None), limit: int = 100):
    with db.get_db() as conn:
        query = (
            "SELECT e.*, s.name AS sequence_name, c.email "
            "FROM sequence_enrollments e "
            "JOIN sequences s ON s.id = e.sequence_id "
            "JOIN contacts c ON c.id = e.contact_id"
        )
        params: list = []
        if status:
            query += " WHERE e.status = ?"
            params.append(status)
        query += " ORDER BY e.created_at DESC LIMIT ?"
        params.append(limit)
        return [db.row_to_dict(r) for r in conn.execute(query, params)]


# ---------- segments ------------------------------------------------------------

@router.get("/segments")
def list_segments():
    from ..segments import evaluate

    with db.get_db() as conn:
        rows = conn.execute("SELECT * FROM segments ORDER BY created_at DESC").fetchall()
        result = []
        for r in rows:
            segment = db.row_to_dict(r)
            segment["rules"] = json.loads(segment["rules"])
            segment["contact_count"] = len(evaluate(segment["rules"]))
            result.append(segment)
        return result


@router.post("/segments", status_code=201)
def create_segment(payload: SegmentIn):
    with db.get_db() as conn:
        segment_id = db.new_id()
        conn.execute(
            "INSERT INTO segments (id, name, rules, created_at) VALUES (?, ?, ?, ?)",
            (segment_id, payload.name, json.dumps(payload.rules), db.now()),
        )
        segment = _or_404(conn.execute("SELECT * FROM segments WHERE id = ?", (segment_id,)).fetchone(), "Segment")
        segment["rules"] = json.loads(segment["rules"])
        return segment


@router.post("/segments/{segment_id}/preview")
def preview_segment(segment_id: str, limit: int = Query(20, le=200)):
    from ..segments import evaluate

    with db.get_db() as conn:
        segment = _or_404(conn.execute("SELECT * FROM segments WHERE id = ?", (segment_id,)).fetchone(), "Segment")
        matched = evaluate(segment["rules"])
        return {"total": len(matched), "contacts": matched[:limit]}


@router.delete("/segments/{segment_id}", status_code=204)
def delete_segment(segment_id: str):
    with db.get_db() as conn:
        if conn.execute("DELETE FROM segments WHERE id = ?", (segment_id,)).rowcount == 0:
            raise HTTPException(404, "Segment not found")


# ---------- A/B tests (email) ----------------------------------------------------

@router.post("/campaigns/{campaign_id}/ab-tests", status_code=201)
def create_ab_test(campaign_id: str, payload: AbTestIn):
    labels = {v.get("label") for v in payload.variants}
    if len(labels) != len(payload.variants):
        raise HTTPException(400, "Variant labels must be unique")
    with db.get_db() as conn:
        campaign = _or_404(conn.execute("SELECT * FROM campaigns WHERE id = ?", (campaign_id,)).fetchone(), "Campaign")
        if campaign["status"] == "sent":
            raise HTTPException(400, "Cannot A/B test a sent campaign")
        try:
            test = ab.create_email_test(conn, payload.name, campaign_id, payload.variants)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return test


@router.get("/ab-tests")
def list_ab_tests():
    with db.get_db() as conn:
        tests = [db.row_to_dict(r) for r in conn.execute("SELECT * FROM ab_tests ORDER BY created_at DESC")]
        for t in tests:
            t["variants"] = [
                db.row_to_dict(v)
                for v in conn.execute("SELECT * FROM ab_variants WHERE test_id = ?", (t["id"],))
            ]
        return tests


@router.post("/ab-tests/{test_id}/send")
def send_ab_test(test_id: str, dry_run: bool = Query(False)):
    with db.get_db() as conn:
        test = _or_404(conn.execute("SELECT * FROM ab_tests WHERE id = ?", (test_id,)).fetchone(), "Test")
        if test["object_type"] != "email":
            raise HTTPException(400, "Only email tests can be sent here")
        if test["status"] != "running":
            raise HTTPException(400, "Test already concluded")
        if not test["campaign_id"]:
            raise HTTPException(400, "Test has no campaign")
        campaign = _or_404(conn.execute("SELECT * FROM campaigns WHERE id = ?", (test["campaign_id"],)).fetchone(), "Campaign")
        variants = [
            db.row_to_dict(v)
            for v in conn.execute("SELECT * FROM ab_variants WHERE test_id = ?", (test_id,))
        ]
        recipients = conn.execute(
            "SELECT * FROM contacts WHERE audience_id = ? AND status = 'subscribed'",
            (campaign["audience_id"],),
        ).fetchall()
        recipients = [dict(r) for r in recipients]
        groups = ab.split_recipients(recipients, variants)

        _ = test  # test row provided to reader as context
        config = mailer.smtp_config()
        results = {}
        for variant in variants:
            label = variant["label"]
            results[label] = {"recipients": len(groups[label])}
            if dry_run or not mailer.is_configured():
                continue
            subject = variant["subject"] or campaign["subject"]
            html = variant["html_content"] or campaign["html_content"]
            for recipient in groups[label]:
                unsub = f"{_public_base()}/unsubscribe?email={recipient['email']}"
                try:
                    mailer.send_email(recipient["email"], subject, html,
                                      unsubscribe_url=unsub)
                except Exception:
                    results[label]["failures"] = results[label].get("failures", 0) + 1
                    continue
                conn.execute(
                    """INSERT INTO campaign_events (id, campaign_id, contact_id, type, timestamp)
                       VALUES (?, ?, ?, 'sent', ?)""",
                    (db.new_id(), campaign["id"], recipient["id"], db.now()),
                )
            conn.execute(
                "UPDATE ab_variants SET recipients = recipients + ? WHERE test_id = ? AND label = ?",
                (len(groups[label]), test_id, label),
            )
        _ = config
        status = "dry_run" if dry_run else ("sent" if mailer.is_configured() else "blocked")
        return {"status": status, "groups": results,
                "smtp_configured": mailer.is_configured()}


@router.post("/ab-tests/{test_id}/winner")
def pick_winner(test_id: str):
    with db.get_db() as conn:
        _or_404(conn.execute("SELECT * FROM ab_tests WHERE id = ?", (test_id,)).fetchone(), "Test")
        return ab.declare_winner(conn, test_id)


@router.get("/ab-tests/{test_id}/report")
def ab_report(test_id: str):
    with db.get_db() as conn:
        test = _or_404(conn.execute("SELECT * FROM ab_tests WHERE id = ?", (test_id,)).fetchone(), "Test")
        variants = [db.row_to_dict(v) for v in
                    conn.execute("SELECT * FROM ab_variants WHERE test_id = ?", (test_id,))]
        for v in variants:
            v["open_rate"] = round(v["opens"] / v["recipients"], 4) if v["recipients"] else 0.0
    return {"test": test, "variants": variants}


def _public_base() -> str:
    from fastapi import Request  # noqa: F401
    import os

    return os.environ.get("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")

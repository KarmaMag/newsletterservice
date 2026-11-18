"""HTTP API for the newsletter service."""
import logging
import os
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, EmailStr, Field

from . import ab, automations, db, mailer
from .events import record_event
from .importer import import_all
from .routers import agent_chat, analytics, commerce, growth, marketing, public, publishing
from .webhooks import dispatch_async

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Newsletter Agent", version="2.0.0")

# Extended platform routers
app.include_router(marketing.router)
app.include_router(publishing.router)
app.include_router(commerce.router)
app.include_router(growth.router)
app.include_router(analytics.router)
app.include_router(agent_chat.router)
app.include_router(public.router)


@app.on_event("startup")
def startup() -> None:
    db.init_db()


# ---------- request / response models --------------------------------------

class AudienceIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class ContactIn(BaseModel):
    email: EmailStr
    audience_id: str
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    tags: list[str] = Field(default_factory=list)
    status: str = "subscribed"
    signup_source: Optional[str] = None


class ContactUpdate(BaseModel):
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    status: Optional[str] = None
    add_tags: list[str] = Field(default_factory=list)
    remove_tags: list[str] = Field(default_factory=list)


class CampaignIn(BaseModel):
    audience_id: str
    name: str = Field(min_length=1, max_length=200)
    subject: str = Field(min_length=1, max_length=300)
    preheader: Optional[str] = None
    from_name: Optional[str] = None
    from_email: Optional[EmailStr] = None
    html_content: str


class CampaignUpdate(BaseModel):
    name: Optional[str] = None
    subject: Optional[str] = None
    preheader: Optional[str] = None
    html_content: Optional[str] = None
    status: Optional[str] = None


class MailchimpImportIn(BaseModel):
    api_key: str = Field(min_length=8)
    with_activity: bool = False
    base_url: Optional[str] = None


class EmailIn(BaseModel):
    email: EmailStr


# ---------- helpers ----------------------------------------------------------

def _audience_or_404(conn, audience_id: str) -> dict:
    row = conn.execute("SELECT * FROM audiences WHERE id = ?", (audience_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Audience not found")
    return db.row_to_dict(row)


def _campaign_or_404(conn, campaign_id: str) -> dict:
    row = conn.execute("SELECT * FROM campaigns WHERE id = ?", (campaign_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Campaign not found")
    return db.row_to_dict(row)


def _contacts_for_campaign(conn, campaign: dict):
    rows = conn.execute(
        "SELECT * FROM contacts WHERE audience_id = ? AND status IN ('subscribed', 'transactional')",
        (campaign["audience_id"],),
    ).fetchall()
    return [db.row_to_dict(r) for r in rows]


def _base_url() -> str:
    return os.environ.get("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")


# ---------- health & stats ----------------------------------------------------

@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/stats")
def stats():
    with db.get_db() as conn:
        audiences = conn.execute("SELECT COUNT(*) c FROM audiences").fetchone()["c"]
        contacts = conn.execute("SELECT COUNT(*) c FROM contacts").fetchone()["c"]
        subscribed = conn.execute(
            "SELECT COUNT(*) c FROM contacts WHERE status = 'subscribed'"
        ).fetchone()["c"]
        campaigns = conn.execute("SELECT COUNT(*) c FROM campaigns").fetchone()["c"]
        sent = conn.execute(
            "SELECT COUNT(*) c FROM campaigns WHERE status = 'sent'"
        ).fetchone()["c"]
        opens = conn.execute(
            "SELECT COUNT(*) c FROM campaign_events WHERE type = 'open'"
        ).fetchone()["c"]
        clicks = conn.execute(
            "SELECT COUNT(*) c FROM campaign_events WHERE type = 'click'"
        ).fetchone()["c"]
        product_revenue = conn.execute(
            "SELECT COALESCE(SUM(amount_cents), 0) c FROM orders WHERE status = 'paid'"
        ).fetchone()["c"]
        active_subs = conn.execute(
            "SELECT COUNT(*) c FROM subscriptions WHERE status = 'active'"
        ).fetchone()["c"]
    return {
        "audiences": audiences,
        "contacts": contacts,
        "subscribed": subscribed,
        "campaigns": campaigns,
        "campaigns_sent": sent,
        "opens": opens,
        "clicks": clicks,
        "product_revenue_cents": product_revenue,
        "active_subscriptions": active_subs,
        "smtp_configured": mailer.is_configured(),
    }


# ---------- audiences ---------------------------------------------------------

@app.get("/api/audiences")
def list_audiences():
    with db.get_db() as conn:
        rows = conn.execute(
            """SELECT a.*, COUNT(c.id) AS contact_count
               FROM audiences a LEFT JOIN contacts c ON c.audience_id = a.id
               GROUP BY a.id ORDER BY a.created_at"""
        ).fetchall()
        return [db.row_to_dict(r) for r in rows]


@app.post("/api/audiences", status_code=201)
def create_audience(payload: AudienceIn):
    with db.get_db() as conn:
        audience_id = db.new_id()
        conn.execute(
            "INSERT INTO audiences (id, name, created_at) VALUES (?, ?, ?)",
            (audience_id, payload.name, db.now()),
        )
        row = conn.execute("SELECT * FROM audiences WHERE id = ?", (audience_id,)).fetchone()
        return db.row_to_dict(row)


@app.delete("/api/audiences/{audience_id}", status_code=204)
def delete_audience(audience_id: str):
    with db.get_db() as conn:
        _audience_or_404(conn, audience_id)
        conn.execute("DELETE FROM contacts WHERE audience_id = ?", (audience_id,))
        conn.execute("DELETE FROM audiences WHERE id = ?", (audience_id,))


# ---------- contacts ----------------------------------------------------------

@app.get("/api/contacts")
def list_contacts(
    audience_id: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    tag: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    with db.get_db() as conn:
        where, params = [], []
        if audience_id:
            where.append("c.audience_id = ?")
            params.append(audience_id)
        if search:
            where.append("(c.email LIKE ? OR c.first_name LIKE ? OR c.last_name LIKE ?)")
            like = f"%{search}%"
            params += [like, like, like]
        if status:
            where.append("c.status = ?")
            params.append(status)
        if tag:
            where.append("c.id IN (SELECT ct.contact_id FROM contact_tags ct "
                         "JOIN tags t ON t.id = ct.tag_id WHERE t.name = ?)")
            params.append(tag)
        where_sql = f" WHERE {' AND '.join(where)}" if where else ""
        total = conn.execute(
            f"SELECT COUNT(*) c FROM contacts c{where_sql}", params
        ).fetchone()["c"]
        rows = conn.execute(
            f"SELECT c.* FROM contacts c{where_sql} ORDER BY c.created_at DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        ).fetchall()
        contacts = [db.export_contact(conn, r["id"]) for r in rows]
    return {"total": total, "contacts": contacts}


@app.post("/api/contacts", status_code=201)
def add_contact(payload: ContactIn):
    with db.get_db() as conn:
        _audience_or_404(conn, payload.audience_id)
        contact = db.upsert_contact(
            conn, payload.audience_id, payload.email,
            payload.first_name, payload.last_name, payload.status,
            signup_source=payload.signup_source,
        )
        for tag in payload.tags:
            db.attach_tag(conn, contact["id"], tag)
            automations.enroll_by_trigger(conn, "tag_added", tag, contact["id"], payload.audience_id)
        record_event(conn, contact["id"], "contact_created",
                     email=payload.email, source=payload.signup_source)
        dispatch_async("contact.created", {"email": payload.email,
                                           "audience_id": payload.audience_id})
        automations.enroll_by_trigger(conn, "signup", payload.signup_source,
                                      contact["id"], payload.audience_id)
        return db.export_contact(conn, contact["id"])


@app.get("/api/contacts/{contact_id}")
def get_contact(contact_id: str):
    with db.get_db() as conn:
        row = conn.execute("SELECT id FROM contacts WHERE id = ?", (contact_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Contact not found")
        return db.export_contact(conn, contact_id)


@app.patch("/api/contacts/{contact_id}")
def update_contact(contact_id: str, payload: ContactUpdate):
    with db.get_db() as conn:
        row = conn.execute("SELECT * FROM contacts WHERE id = ?", (contact_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Contact not found")
        if payload.first_name is not None:
            conn.execute("UPDATE contacts SET first_name = ? WHERE id = ?",
                         (payload.first_name, contact_id))
        if payload.last_name is not None:
            conn.execute("UPDATE contacts SET last_name = ? WHERE id = ?",
                         (payload.last_name, contact_id))
        if payload.status is not None:
            conn.execute(
                "UPDATE contacts SET status = ?, unsubscribed_at = ? WHERE id = ?",
                (payload.status, db.now() if payload.status == "unsubscribed" else None,
                 contact_id),
            )
        for tag in payload.add_tags:
            db.attach_tag(conn, contact_id, tag)
        for tag in payload.remove_tags:
            conn.execute(
                """DELETE FROM contact_tags WHERE contact_id = ?
                   AND tag_id IN (SELECT id FROM tags WHERE name = ?)""",
                (contact_id, tag),
            )
        return db.export_contact(conn, contact_id)


@app.delete("/api/contacts/{contact_id}", status_code=204)
def delete_contact(contact_id: str):
    with db.get_db() as conn:
        result = conn.execute("DELETE FROM contacts WHERE id = ?", (contact_id,))
        if result.rowcount == 0:
            raise HTTPException(404, "Contact not found")


@app.post("/api/unsubscribe")
def unsubscribe(payload: EmailIn):
    with db.get_db() as conn:
        result = conn.execute(
            "UPDATE contacts SET status = 'unsubscribed', unsubscribed_at = ? WHERE email = ?",
            (db.now(), payload.email.lower()),
        )
        if result.rowcount == 0:
            raise HTTPException(404, "Contact not found")
        record_event(conn, None, "unsubscribed", email=payload.email.lower())
        dispatch_async("contact.unsubscribed", {"email": payload.email})
    return {"status": "unsubscribed", "email": payload.email}


@app.get("/api/tags")
def list_tags():
    with db.get_db() as conn:
        rows = conn.execute(
            """SELECT t.name, COUNT(ct.contact_id) AS contact_count
               FROM tags t LEFT JOIN contact_tags ct ON ct.tag_id = t.id
               GROUP BY t.id ORDER BY t.name"""
        ).fetchall()
        return [db.row_to_dict(r) for r in rows]


# ---------- Mailchimp import ---------------------------------------------------

@app.post("/api/import/mailchimp")
def import_from_mailchimp(payload: MailchimpImportIn):
    try:
        report = import_all(
            payload.api_key, with_activity=payload.with_activity,
            base_url=payload.base_url,
        )
    except Exception as exc:
        raise HTTPException(400, f"Mailchimp import failed: {exc}")
    return report


# ---------- campaigns ----------------------------------------------------------

@app.get("/api/campaigns")
def list_campaigns():
    with db.get_db() as conn:
        rows = conn.execute("SELECT * FROM campaigns ORDER BY created_at DESC").fetchall()
        return [db.row_to_dict(r) for r in rows]


@app.post("/api/campaigns", status_code=201)
def create_campaign(payload: CampaignIn):
    with db.get_db() as conn:
        _audience_or_404(conn, payload.audience_id)
        campaign_id = db.new_id()
        conn.execute(
            """INSERT INTO campaigns
                 (id, audience_id, name, subject, preheader, from_name, from_email,
                  html_content, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'draft', ?)""",
            (campaign_id, payload.audience_id, payload.name, payload.subject,
             payload.preheader, payload.from_name, payload.from_email,
             payload.html_content, db.now()),
        )
        return _campaign_or_404(conn, campaign_id)


@app.get("/api/campaigns/{campaign_id}")
def get_campaign(campaign_id: str):
    with db.get_db() as conn:
        campaign = _campaign_or_404(conn, campaign_id)
        campaign["recipient_count"] = len(_contacts_for_campaign(conn, campaign))
        return campaign


@app.patch("/api/campaigns/{campaign_id}")
def update_campaign(campaign_id: str, payload: CampaignUpdate):
    with db.get_db() as conn:
        current = _campaign_or_404(conn, campaign_id)
        if current["status"] == "sent" and (payload.subject or payload.html_content
                                            or payload.name):
            raise HTTPException(400, "Cannot edit a sent campaign")
        updates = payload.model_dump(exclude_none=True)
        fields = {k: v for k, v in updates.items() if k in
                  ("name", "subject", "preheader", "html_content", "status")}
        for key, value in fields.items():
            conn.execute(f"UPDATE campaigns SET {key} = ? WHERE id = ?", (value, campaign_id))
        return _campaign_or_404(conn, campaign_id)


@app.delete("/api/campaigns/{campaign_id}", status_code=204)
def delete_campaign(campaign_id: str):
    with db.get_db() as conn:
        campaign = _campaign_or_404(conn, campaign_id)
        if campaign["status"] == "sent":
            raise HTTPException(400, "Cannot delete a sent campaign")
        conn.execute("DELETE FROM campaign_events WHERE campaign_id = ?", (campaign_id,))
        conn.execute("DELETE FROM campaigns WHERE id = ?", (campaign_id,))


@app.post("/api/campaigns/{campaign_id}/preview")
def preview_campaign(campaign_id: str, payload: EmailIn, limit: int = 1):
    """Send a test copy of a campaign to the given address."""
    with db.get_db() as conn:
        campaign = _campaign_or_404(conn, campaign_id)
    unsub = f"{_base_url()}/unsubscribe?email={payload.email}"
    try:
        mailer.send_email(
            payload.email, campaign["subject"], campaign["html_content"],
            campaign["preheader"], unsubscribe_url=unsub,
        )
    except mailer.MailerConfigError as exc:
        raise HTTPException(400, str(exc))
    except RuntimeError as exc:
        raise HTTPException(502, str(exc))
    return {"status": "test_sent", "to": payload.email,
            "from": mailer.smtp_config()["from_email"]}


@app.post("/api/campaigns/{campaign_id}/send")
def send_campaign(campaign_id: str, dry_run: bool = Query(False)):
    """Send a draft campaign to all subscribed contacts of its audience."""
    with db.get_db() as conn:
        campaign = _campaign_or_404(conn, campaign_id)
        recipients = _contacts_for_campaign(conn, campaign)
        if dry_run or not mailer.is_configured():
            return {
                "status": "dry_run" if dry_run else "blocked",
                "recipients": len(recipients),
                "smtp_configured": mailer.is_configured(),
                "message": "No emails sent. Configure SMTP or pass ?dry_run=true "
                           "to simulate the send.",
            }
        conn.execute(
            "UPDATE campaigns SET status = 'sent', sent_at = ? WHERE id = ?",
            (db.now(), campaign_id),
        )
        test = ab.test_for_campaign(conn, campaign_id)
        groups = None
        if test:
            variants = [db.row_to_dict(v) for v in conn.execute(
                "SELECT * FROM ab_variants WHERE test_id = ?", (test["id"],)
            )]
            groups = ab.split_recipients(recipients, variants)
        for recipient in recipients:
            conn.execute(
                """INSERT INTO campaign_events (id, campaign_id, contact_id, type, timestamp)
                   VALUES (?, ?, ?, 'sent', ?)""",
                (db.new_id(), campaign_id, recipient["id"], db.now()),
            )
        if groups:
            for label, members in groups.items():
                conn.execute(
                    "UPDATE ab_variants SET recipients = recipients + ? "
                    "WHERE test_id = ? AND label = ?",
                    (len(members), test["id"], label),
                )
    sent_html = campaign["html_content"]
    from_name = campaign["from_name"] or mailer.smtp_config()["from_name"]
    from_email = campaign["from_email"] or mailer.smtp_config()["from_email"]
    failures = []
    groups_by_email = {}
    variants_content = {}
    if groups:
        for label, members in groups.items():
            for m in members:
                groups_by_email[m["email"]] = label
            variants_content[label] = (campaign["subject"], sent_html)
        for v_row in _active_variants(test["id"]):
            subject, html = v_row["subject"], v_row["html_content"]
            variants_content[v_row["label"]] = (
                subject or campaign["subject"], html or sent_html,
            )
    for recipient in recipients:
        label = groups_by_email.get(recipient["email"])
        subject, html = campaign["subject"], sent_html
        if label:
            subject, html = variants_content.get(label, (subject, html))
        unsub = f"{_base_url()}/unsubscribe?email={recipient['email']}"
        try:
            mailer.send_email(
                recipient["email"], subject, html,
                campaign["preheader"], unsubscribe_url=unsub,
            )
        except Exception as exc:
            failures.append(recipient["email"])
            logger.warning("Send to %s failed: %s", recipient["email"], exc)
    result = {"status": "sent", "recipients": len(recipients), "failures": failures,
              "from": f"{from_name} <{from_email}>"}
    if groups:
        result["ab_groups"] = {label: len(members) for label, members in groups.items()}
        dispatch_async("campaign.sent", {"campaign_id": campaign_id,
                                         "recipients": len(recipients),
                                         "ab_groups": result["ab_groups"]})
    return result


def _active_variants(test_id: str) -> list[dict]:
    with db.get_db() as conn:
        rows = conn.execute("SELECT * FROM ab_variants WHERE test_id = ?", (test_id,)).fetchall()
        return [db.row_to_dict(r) for r in rows]


@app.get("/api/campaigns/{campaign_id}/stats")
def campaign_stats(campaign_id: str):
    with db.get_db() as conn:
        campaign = _campaign_or_404(conn, campaign_id)
        counts = {
            row["type"]: row["c"]
            for row in conn.execute(
                "SELECT type, COUNT(*) c FROM campaign_events "
                "WHERE campaign_id = ? GROUP BY type", (campaign_id,)
            )
        }
    sent = counts.get("sent", 0)
    opens = counts.get("open", 0)
    clicks = counts.get("click", 0)
    return {
        "campaign_id": campaign_id,
        "status": campaign["status"],
        "sent": sent,
        "opens": opens,
        "clicks": clicks,
        "open_rate": round(opens / sent, 4) if sent else 0.0,
        "click_rate": round(clicks / sent, 4) if sent else 0.0,
    }


@app.post("/api/campaigns/{campaign_id}/track/{event_type}")
def track_event(campaign_id: str, event_type: str, payload: EmailIn):
    """Record engagement (e.g. from webhooks or the pixel link). Mainly open/click."""
    if event_type not in ("open", "click", "bounce", "complaint"):
        raise HTTPException(400, "Invalid event type")
    with db.get_db() as conn:
        campaign = _campaign_or_404(conn, campaign_id)
        row = conn.execute(
            "SELECT id FROM contacts WHERE email = ?", (payload.email.lower(),)
        ).fetchone()
        if not row:
            raise HTTPException(404, "Contact not found")
        conn.execute(
            """INSERT INTO campaign_events (id, campaign_id, contact_id, type, timestamp)
               VALUES (?, ?, ?, ?, ?)""",
            (db.new_id(), campaign["id"], row["id"], event_type, db.now()),
        )
    return {"status": "recorded", "type": event_type}


# ---------- web UI -------------------------------------------------------------

@app.get("/unsubscribe")
def unsubscribe_page(email: str):
    return FileResponse(os.path.join(_static_dir, "unsubscribe.html"))


@app.get("/open/{campaign_id}/{email}")
def open_pixel(campaign_id: str, email: str, v: Optional[str] = None):
    from fastapi.responses import Response
    with db.get_db() as conn:
        campaign = _campaign_or_404(conn, campaign_id)
        row = conn.execute("SELECT id FROM contacts WHERE email = ?", (email.lower(),)).fetchone()
        if row:
            conn.execute(
                """INSERT INTO campaign_events (id, campaign_id, contact_id, type, timestamp)
                   VALUES (?, ?, ?, 'open', ?)""",
                (db.new_id(), campaign["id"], row["id"], db.now()),
            )
            ab.record_variant_open(conn, campaign_id, email)
    _ = v
    return Response(
        b"\x89PNG\r\n\x1a\n" + bytes(64),
        media_type="image/png",
        headers={"Cache-Control": "no-store"},
    )


_static_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "static"))
app.mount("/static", StaticFiles(directory=_static_dir), name="static")


@app.get("/")
def index():
    return FileResponse(os.path.join(_static_dir, "index.html"))

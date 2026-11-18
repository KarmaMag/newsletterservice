"""Growth router: referrals, Creator Network recommendations, sponsorships,
integrations/webhooks directory."""
import json
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, EmailStr

from .. import db, webhooks
from ..events import record_event

router = APIRouter(prefix="/api")


class RecommendationIn(BaseModel):
    partner_name: str
    partner_url: str
    description: str = ""
    owner_newsletter: str = ""


class SponsorshipIn(BaseModel):
    sponsor_name: str
    sponsor_url: Optional[str] = None
    ad_copy: str = ""
    price_cents: int = 0


class BookSponsorshipIn(BaseModel):
    campaign_id: Optional[str] = None
    booked_by_email: EmailStr


class WebhookIn(BaseModel):
    name: str
    url: str
    events: list[str] = []
    secret: str = ""
    active: bool = True


def _or_404(row, what: str) -> dict:
    if not row:
        raise HTTPException(404, f"{what} not found")
    return db.row_to_dict(row)


# ---------- referrals ---------------------------------------------------------------

@router.get("/referrals/{contact_id}")
def referral_overview(contact_id: str):
    with db.get_db() as conn:
        contact = _or_404(conn.execute("SELECT id, email FROM contacts WHERE id = ?", (contact_id,)).fetchone(), "Contact")
        code = contact["id"]
        referred = []
        for r in conn.execute(
            "SELECT referred_email, created_at FROM referrals WHERE referrer_contact_id = ? ORDER BY created_at",
            (contact_id,),
        ):
            row = conn.execute("SELECT id FROM contacts WHERE email = ?", (r["referred_email"],)).fetchone()
            subscribed = bool(row)
            referred.append({**db.row_to_dict(r), "subscribed": subscribed})
        return {
            "referral_link": f"{_public_base()}/r/{code}",
            "code": code,
            "referred": referred,
            "signup_count": len(referred),
        }


@router.get("/referrals/leaderboard/top")
def referral_leaderboard(limit: int = 10):
    with db.get_db() as conn:
        rows = conn.execute(
            """SELECT r.referrer_contact_id, c.email, COUNT(*) AS directs,
                      SUM(CASE WHEN rc.id IS NOT NULL THEN 1 ELSE 0 END) AS subscribed
               FROM referrals r
               LEFT JOIN contacts c ON c.id = r.referrer_contact_id
               LEFT JOIN contacts rc ON rc.email = r.referred_email
               GROUP BY r.referrer_contact_id ORDER BY directs DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [db.row_to_dict(r) for r in rows]


# ---------- Creator Network ----------------------------------------------------------

@router.get("/recommendations")
def list_recommendations(active_only: bool = False):
    with db.get_db() as conn:
        query = "SELECT * FROM creator_recommendations"
        if active_only:
            query += " WHERE active = 1"
        query += " ORDER BY created_at DESC"
        return [db.row_to_dict(r) for r in conn.execute(query)]


@router.post("/recommendations", status_code=201)
def add_recommendation(payload: RecommendationIn):
    with db.get_db() as conn:
        rec_id = db.new_id()
        conn.execute(
            """INSERT INTO creator_recommendations
                 (id, owner_newsletter, partner_name, partner_url, description, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (rec_id, payload.owner_newsletter, payload.partner_name, payload.partner_url,
             payload.description, db.now()),
        )
        return _or_404(conn.execute("SELECT * FROM creator_recommendations WHERE id = ?", (rec_id,)).fetchone(), "Recommendation")


@router.delete("/recommendations/{rec_id}", status_code=204)
def delete_recommendation(rec_id: str):
    with db.get_db() as conn:
        if conn.execute("DELETE FROM creator_recommendations WHERE id = ?", (rec_id,)).rowcount == 0:
            raise HTTPException(404, "Recommendation not found")


@router.post("/recommendations/embed")
def recommendation_embed():
    """HTML snippet to paste at the bottom of any newsletter."""
    with db.get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM creator_recommendations WHERE active = 1 ORDER BY created_at DESC"
        ).fetchall()
        if not rows:
            raise HTTPException(404, "No active recommendations to embed")
        links = "".join(
            f'<p style="margin:4px 0"><b>{r["partner_name"]}</b> — '
            f'<a href="{_public_base()}/r/reco/{r["id"]}">{r["description"] or r["partner_name"]}</a></p>'
            for r in rows
        )
        for r in rows:
            conn.execute("UPDATE creator_recommendations SET impressions = impressions + 1 WHERE id = ?", (r["id"],))
        base = _public_base()
        return {"html": f'<div style="border-top:1px solid #ddd;margin-top:24px;padding-top:12px">'
                        f'<p style="font-size:13px;color:#666"><b>Recommended reading</b></p>{links}</div>',
                "count": len(rows)}


# ---------- sponsorships -------------------------------------------------------------

@router.get("/sponsorships")
def list_sponsorships():
    with db.get_db() as conn:
        return [db.row_to_dict(r) for r in
                conn.execute("SELECT * FROM sponsorships ORDER BY created_at DESC")]


@router.post("/sponsorships", status_code=201)
def create_sponsorship(payload: SponsorshipIn):
    with db.get_db() as conn:
        slot_id = db.new_id()
        conn.execute(
            """INSERT INTO sponsorships (id, sponsor_name, sponsor_url, ad_copy, price_cents, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (slot_id, payload.sponsor_name, payload.sponsor_url, payload.ad_copy,
             payload.price_cents, db.now()),
        )
        return _or_404(conn.execute("SELECT * FROM sponsorships WHERE id = ?", (slot_id,)).fetchone(), "Sponsorship")


@router.post("/sponsorships/{slot_id}/book")
def book_sponsorship(slot_id: str, payload: BookSponsorshipIn):
    with db.get_db() as conn:
        _or_404(conn.execute("SELECT * FROM sponsorships WHERE id = ?", (slot_id,)).fetchone(), "Sponsorship")
        conn.execute(
            "UPDATE sponsorships SET status = 'sold', campaign_id = ?, booked_by_email = ? WHERE id = ?",
            (payload.campaign_id, payload.booked_by_email, slot_id),
        )
        webhooks.dispatch_async("sponsorship.sold",
                                {"sponsorship_id": slot_id, "email": payload.booked_by_email})
        return _or_404(conn.execute("SELECT * FROM sponsorships WHERE id = ?", (slot_id,)).fetchone(), "Sponsorship")


@router.post("/sponsorships/{slot_id}/deliver")
def deliver_sponsorship(slot_id: str):
    with db.get_db() as conn:
        _or_404(conn.execute("SELECT * FROM sponsorships WHERE id = ?", (slot_id,)).fetchone(), "Sponsorship")
        conn.execute("UPDATE sponsorships SET status = 'delivered' WHERE id = ?", (slot_id,))
        return {"status": "delivered"}


@router.get("/sponsorships/revenue/summary")
def sponsorship_revenue():
    with db.get_db() as conn:
        sold = conn.execute(
            "SELECT COALESCE(SUM(price_cents), 0) c FROM sponsorships WHERE status IN ('sold', 'delivered')"
        ).fetchone()["c"]
        available = conn.execute(
            "SELECT COUNT(*) c FROM sponsorships WHERE status = 'available'"
        ).fetchone()["c"]
        return {"total_cents": sold, "available_slots": available}


# ---------- integrations / webhooks ----------------------------------------------------

INTEGRATION_DIRECTORY = [
    {"name": "Zapier", "direction": "outbound", "setup": "Create a webhook with event 'webhook.test' to your Zapier hook URL"},
    {"name": "Stripe", "direction": "commerce", "note": "Subscriptions and orders currently use the internal mock provider; replace commerce.create_subscription/create_order with Stripe checkout calls"},
    {"name": "Calendly-type", "direction": "bookings", "note": "Native booking pages exist under /book/{service}; SMTP confirmations on request"},
    {"name": "Generic webhook", "direction": "outbound", "setup": "Register any URL in /api/integrations/webhooks; signed with HMAC-SHA256 (X-Newsletter-Signature)"},
]

WEBHOOK_EVENTS = [
    "contact.created", "contact.unsubscribed", "campaign.sent",
    "purchase", "subscription_started", "sequence_enrolled",
    "booking_confirmed", "form_signup", "sponsorship.sold",
]


@router.get("/integrations/directory")
def integrations_directory():
    return {"webhook_events": WEBHOOK_EVENTS, "integrations": INTEGRATION_DIRECTORY}


@router.get("/integrations/webhooks")
def list_webhooks():
    with db.get_db() as conn:
        rows = conn.execute("SELECT * FROM webhooks ORDER BY created_at DESC").fetchall()
        result = []
        for r in rows:
            hook = db.row_to_dict(r)
            hook["events"] = json.loads(hook["events"])
            result.append(hook)
        return result


@router.post("/integrations/webhooks", status_code=201)
def create_webhook(payload: WebhookIn):
    with db.get_db() as conn:
        unknown = [e for e in payload.events if e not in WEBHOOK_EVENTS]
        if unknown:
            raise HTTPException(400, f"Unknown events: {unknown}")
        webhook_id = db.new_id()
        conn.execute(
            """INSERT INTO webhooks (id, name, url, events, secret, active, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (webhook_id, payload.name, payload.url, json.dumps(payload.events),
             payload.secret, int(payload.active), db.now()),
        )
        return _or_404(conn.execute("SELECT * FROM webhooks WHERE id = ?", (webhook_id,)).fetchone(), "Webhook")


@router.delete("/integrations/webhooks/{webhook_id}", status_code=204)
def delete_webhook(webhook_id: str):
    with db.get_db() as conn:
        if conn.execute("DELETE FROM webhooks WHERE id = ?", (webhook_id,)).rowcount == 0:
            raise HTTPException(404, "Webhook not found")


@router.post("/integrations/webhooks/{webhook_id}/test")
def test_webhook(webhook_id: str):
    import httpx

    with db.get_db() as conn:
        hook = _or_404(conn.execute("SELECT * FROM webhooks WHERE id = ?", (webhook_id,)).fetchone(), "Webhook")
        try:
            status = webhooks.dispatch(conn, "webhook.test", {"webhook": hook["name"]})
            record_event(conn, None, "webhook_test", value=hook["name"])
        except httpx.HTTPError as exc:
            raise HTTPException(502, f"Webhook delivery failed: {exc}")
    return {"delivered_to": status}


def _public_base() -> str:
    import os

    return os.environ.get("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")

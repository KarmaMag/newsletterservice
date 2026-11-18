"""AI chat endpoint: analytics & strategy discussions about the newsletter."""
import os
import threading

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/api/agent")


class ChatIn(BaseModel):
    message: str
    history: list[dict] = []


class ChatOut(BaseModel):
    reply: str
    ai_powered: bool


def _fallback_insights(message: str) -> str:
    from .. import db

    with db.get_db() as conn:
        contacts = conn.execute("SELECT COUNT(*) c FROM contacts").fetchone()["c"]
        subscribed = conn.execute(
            "SELECT COUNT(*) c FROM contacts WHERE status = 'subscribed'"
        ).fetchone()["c"]
        sends = conn.execute(
            "SELECT COUNT(*) c FROM campaign_events WHERE type = 'sent'"
        ).fetchone()["c"]
        opens = conn.execute(
            "SELECT COUNT(*) c FROM campaign_events WHERE type = 'open'"
        ).fetchone()["c"]
        clicks = conn.execute(
            "SELECT COUNT(*) c FROM campaign_events WHERE type = 'click'"
        ).fetchone()["c"]
    open_rate = (opens / sends * 100) if sends else 0.0
    click_rate = (clicks / sends * 100) if sends else 0.0
    lines = [
        "AI mode is off (no MISTRAL_API_KEY), here is a data snapshot instead:",
        f" • {subscribed} of {contacts} contacts subscribed",
    ]
    if sends:
        lines.append(f" • open rate {open_rate:.1f}%, click rate {click_rate:.1f}% across {sends} sends")
    else:
        lines.append(" • no sends yet")
    if "grow" in message.lower() or "strategy" in message.lower():
        lines.append(" • growth levers: referral program (/referrals), landing pages with A/B"
                     " tests, a welcome sequence, and the Creator Network embed")
    if "revenue" in message.lower() or "monetiz" in message.lower():
        lines.append(" • revenue levers: paid subscription tiers, digital products,"
                     " sponsorship slots (see Commerce and Sponsorships)")
    return "\n".join(lines)


@router.post("/chat", response_model=ChatOut)
def chat(payload: ChatIn):
    if not payload.message.strip():
        raise HTTPException(400, "Message is empty")
    api_key = os.environ.get("MISTRAL_API_KEY")
    if not api_key:
        return ChatOut(reply=_fallback_insights(payload.message), ai_powered=False)
    try:
        from ..agent.agent import run_agent

        reply = run_agent(payload.message, payload.history)
    except Exception as exc:
        raise HTTPException(502, f"AI agent failed: {exc}")
    return ChatOut(reply=reply, ai_powered=True)

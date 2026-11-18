"""Analytics router: growth, engagement, revenue, funnel and creator metrics."""
from fastapi import APIRouter, Query

from .. import db

router = APIRouter(prefix="/api/analytics")


def _daily_counts(conn, table: str, ts_col: str, extra_where: str = "",
                  params: tuple = (), days: int = 30) -> dict:
    import datetime as dt

    since = db.now() - days * 86400
    rows = conn.execute(
        f"SELECT {ts_col} FROM {table} WHERE {ts_col} >= ? {extra_where}",
        (since, *params),
    ).fetchall()
    buckets = {}
    for r in rows:
        day = dt.datetime.utcfromtimestamp(r[0]).date().isoformat()
        buckets[day] = buckets.get(day, 0) + 1
    return buckets


@router.get("/overview")
def overview(days: int = Query(30, ge=1, le=365)):
    with db.get_db() as conn:
        total_contacts = conn.execute("SELECT COUNT(*) c FROM contacts").fetchone()["c"]
        subscribed = conn.execute(
            "SELECT COUNT(*) c FROM contacts WHERE status = 'subscribed'"
        ).fetchone()["c"]
        contact_growth = _daily_counts(conn, "contacts", "created_at", days=days)
        opens_by_day = _daily_counts(conn, "campaign_events", "timestamp",
                                     "AND type = 'open'", days=days)
        clicks_by_day = _daily_counts(conn, "campaign_events", "timestamp",
                                      "AND type = 'click'", days=days)
        campaigns_sent = conn.execute(
            "SELECT COUNT(*) c FROM campaigns WHERE status = 'sent'"
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
        product_revenue = conn.execute(
            "SELECT COALESCE(SUM(amount_cents), 0) c FROM orders WHERE status = 'paid'"
        ).fetchone()["c"]
        sponsor_revenue = conn.execute(
            "SELECT COALESCE(SUM(price_cents), 0) c FROM sponsorships WHERE status IN ('sold','delivered')"
        ).fetchone()["c"]
        bookings = conn.execute(
            "SELECT COUNT(*) c FROM bookings WHERE status = 'booked'"
        ).fetchone()["c"]
        page_views = conn.execute(
            "SELECT COUNT(*) c FROM subscriber_events WHERE type = 'page_view'"
        ).fetchone()["c"]
        form_signups = conn.execute(
            "SELECT COUNT(*) c FROM subscriber_events WHERE type = 'form_signup'"
        ).fetchone()["c"]
        purchases = conn.execute(
            "SELECT COUNT(*) c FROM subscriber_events WHERE type = 'purchase'"
        ).fetchone()["c"]
        active_subs = conn.execute(
            "SELECT COUNT(*) c FROM subscriptions WHERE status = 'active'"
        ).fetchone()["c"]
        top_sources = [
            db.row_to_dict(r) for r in conn.execute(
                "SELECT COALESCE(signup_source, '(none)') AS source, COUNT(*) c FROM contacts "
                "GROUP BY signup_source ORDER BY c DESC LIMIT 10"
            )
        ]
        top_campaigns = [
            db.row_to_dict(r) for r in conn.execute(
                """SELECT c.name,
                          SUM(CASE WHEN e.type = 'sent' THEN 1 ELSE 0 END) sent,
                          SUM(CASE WHEN e.type = 'open' THEN 1 ELSE 0 END) opens,
                          SUM(CASE WHEN e.type = 'click' THEN 1 ELSE 0 END) clicks
                   FROM campaigns c LEFT JOIN campaign_events e ON e.campaign_id = c.id
                   GROUP BY c.id ORDER BY opens DESC LIMIT 5"""
            )
        ]
    return {
        "period_days": days,
        "audience": {
            "total_contacts": total_contacts,
            "subscribed": subscribed,
            "growth_by_day": contact_growth,
            "top_signup_sources": top_sources,
        },
        "engagement": {
            "campaigns_sent": campaigns_sent,
            "sends": sends,
            "opens": opens,
            "clicks": clicks,
            "open_rate": round(opens / sends, 4) if sends else 0.0,
            "click_rate": round(clicks / sends, 4) if sends else 0.0,
            "opens_by_day": opens_by_day,
            "clicks_by_day": clicks_by_day,
            "top_campaigns": top_campaigns,
        },
        "revenue": {
            "product_revenue_cents": product_revenue,
            "sponsorship_revenue_cents": sponsor_revenue,
            "active_subscriptions": active_subs,
            "total_cents": product_revenue + sponsor_revenue,
        },
        "funnel": {
            "page_views": page_views,
            "form_signups": form_signups,
            "purchases": purchases,
            "bookings": bookings,
            "view_to_signup_rate": round(form_signups / page_views, 4) if page_views else 0.0,
            "signup_to_purchase_rate": round(purchases / form_signups, 4) if form_signups else 0.0,
        },
    }


@router.get("/creator")
def creator_dashboard():
    with db.get_db() as conn:
        subscribers = conn.execute(
            "SELECT COUNT(*) c FROM contacts WHERE status IN ('subscribed','pending')"
        ).fetchone()["c"]
        revenue = conn.execute(
            "SELECT COALESCE(SUM(amount_cents), 0) c FROM orders WHERE status = 'paid'"
        ).fetchone()["c"]
        sponsor = conn.execute(
            "SELECT COALESCE(SUM(price_cents), 0) c FROM sponsorships WHERE status IN ('sold','delivered')"
        ).fetchone()["c"]
        open_subs = conn.execute(
            "SELECT COUNT(*) c FROM subscriptions WHERE status = 'active'"
        ).fetchone()["c"]
        mrr = conn.execute(
            """SELECT COALESCE(SUM(CASE WHEN p.interval = 'yearly' THEN p.price_cents / 12.0
                                        ELSE p.price_cents END), 0) c
               FROM subscriptions s JOIN subscription_plans p ON p.id = s.plan_id
               WHERE s.status = 'active'"""
        ).fetchone()["c"]
        published_posts = conn.execute(
            "SELECT COUNT(*) c FROM posts WHERE status = 'published'"
        ).fetchone()["c"]
        rec_clicks = conn.execute(
            "SELECT COALESCE(SUM(clicks), 0) c FROM creator_recommendations"
        ).fetchone()["c"]
        referral_signups = conn.execute(
            "SELECT COUNT(*) c FROM referrals"
        ).fetchone()["c"]
    return {
        "subscribers": subscribers,
        "published_posts": published_posts,
        "reader_revenue_cents": revenue,
        "sponsorship_revenue_cents": sponsor,
        "active_paid_subscriptions": open_subs,
        "estimated_mrr_cents": round(mrr),
        "creator_network_clicks": rec_clicks,
        "referral_signups": referral_signups,
    }

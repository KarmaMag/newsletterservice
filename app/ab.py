"""A/B testing for campaigns and landing pages.

Email tests: two or more variants each with their own subject (and optional HTML);
recipients are split deterministically by email hash, sends/opens/clicks are
attributed to the variant label, and a winner can be declared explicitly or on a
timeout rule (highest open rate once all recipients are counted).

Landing page tests: variants rotate by visitor hash, view/conversion events are
recorded, and the same winner rule applies.
"""
import hashlib

from . import db
from .events import record_event


def assign(email: str, n: int) -> int:
    digest = hashlib.md5(email.strip().lower().encode()).digest()
    return digest[0] % max(1, n)


def create_email_test(conn, name: str, campaign_id: str, variants: list[dict]) -> dict:
    """variants: [{"label": "A", "subject": "...", "html": "..."}, ...]"""
    if len(variants) < 2:
        raise ValueError("An A/B test needs at least 2 variants")
    test_id = db.new_id()
    conn.execute(
        """INSERT INTO ab_tests (id, name, campaign_id, object_type, metric, created_at)
           VALUES (?, ?, ?, 'email', 'open_rate', ?)""",
        (test_id, name, campaign_id, db.now()),
    )
    for v in variants:
        conn.execute(
            """INSERT INTO ab_variants (id, test_id, label, subject, html_content)
               VALUES (?, ?, ?, ?, ?)""",
            (db.new_id(), test_id, v["label"], v["subject"], v.get("html") or ""),
        )
    row = conn.execute("SELECT * FROM ab_tests WHERE id = ?", (test_id,)).fetchone()
    return db.row_to_dict(row)


def test_for_campaign(conn, campaign_id: str):
    return conn.execute(
        "SELECT * FROM ab_tests WHERE campaign_id = ? AND status = 'running'",
        (campaign_id,),
    ).fetchone()


def split_recipients(recipients: list[dict], variants: list[dict]) -> dict:
    """Group recipients by variant label using a stable hash split."""
    groups: dict[str, list[dict]] = {v["label"]: [] for v in variants}
    for recipient in recipients:
        labels = [v["label"] for v in variants]
        labels.sort()
        chosen = labels[assign(recipient["email"], len(labels))]
        groups[chosen].append(recipient)
    return groups


def record_variant_open(conn, campaign_id: str, email: str) -> None:
    test = conn.execute(
        "SELECT * FROM ab_tests WHERE campaign_id = ? AND object_type = 'email'",
        (campaign_id,),
    ).fetchone()
    if not test:
        return
    variants = conn.execute(
        "SELECT * FROM ab_variants WHERE test_id = ?", (test["id"],)
    ).fetchall()
    labels = sorted(v["label"] for v in variants)
    chosen = labels[assign(email, len(labels))]
    conn.execute(
        "UPDATE ab_variants SET opens = opens + 1 WHERE test_id = ? AND label = ?",
        (test["id"], chosen),
    )


def declare_winner(conn, test_id: str) -> dict:
    rows = conn.execute(
        "SELECT * FROM ab_variants WHERE test_id = ? ORDER BY label", (test_id,)
    ).fetchall()
    best = None
    for row in rows:
        rate = row["opens"] / row["recipients"] if row["recipients"] else 0.0
        if best is None or rate > best["rate"]:
            if best is None or rate > best["rate"] or (rate == best["rate"] and row["label"] < best["label"]):
                best = {"label": row["label"], "rate": rate}
    conn.execute(
        "UPDATE ab_tests SET status = 'done', winner_label = ? WHERE id = ?",
        (best["label"] if best else None, test_id),
    )
    return {"winner": best["label"] if best else None, "rates": {
        r["label"]: round(r["opens"] / r["recipients"], 4) if r["recipients"] else 0.0
        for r in rows
    }}


def page_variant_for(conn, page_id: str, visitor_email: str | None) -> dict | None:
    """Pick a variant for a page view; falls back to the page body when untested."""
    variants = conn.execute(
        "SELECT * FROM page_variants WHERE page_id = ? ORDER BY label", (page_id,)
    ).fetchall()
    if not variants:
        return None
    total_weight = sum(v["weight"] for v in variants) or 1
    visitor_key = visitor_email or db.new_id()
    point = assign(visitor_key, 1000) / 1000 * total_weight
    cumulative = 0.0
    for v in variants:
        cumulative += v["weight"]
        if point < cumulative:
            return dict(v)
    return dict(variants[0])

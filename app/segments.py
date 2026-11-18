"""Rule-based segment evaluation over contacts.

A segment is a list of rules, all of which must match (AND). Supported rules:

    {"field": "tag",          "op": "any",      "value": "vip"}
    {"field": "status",       "op": "eq",       "value": "subscribed"}
    {"field": "email",        "op": "contains", "value": "@gmail.com"}
    {"field": "event",        "op": "has",      "value": "purchase"}
    {"field": "signup_source","op": "eq",       "value": "landing:authors"}
    {"field": "campaign",     "op": "opened",   "value": "<campaign_id>"}
    {"field": "sequence",     "op": "enrolled", "value": "<sequence_id>"}
"""
import json

from . import db
from . import events


def _match(rule: dict, contact: dict, ctx: dict) -> bool:
    field, op, value = rule.get("field"), rule.get("op"), rule.get("value")

    if field == "tag":
        tags = ctx["tags"]
        if op == "any":
            return bool(value and value in tags)
        if op == "not_any":
            return not (value and value in tags)
        return False

    if field == "event":
        return bool(value and value in ctx["event_types"])

    if field == "campaign":
        statuses = ctx["campaign_actions"].get(contact["id"], set())
        return f"{op}:{value}" in statuses

    if field == "sequence":
        sequences = ctx["enrollments"].get(contact["id"], set())
        return value in sequences

    actual = {
        "status": contact["status"],
        "email": contact["email"],
        "signup_source": contact.get("signup_source") or "",
        "audience_id": contact["audience_id"],
    }.get(field)

    if actual is None:
        return False
    if op == "eq":
        return actual == value
    if op == "neq":
        return actual != value
    if op == "contains":
        return bool(value and value.lower() in actual.lower())
    return False


def _context(contact_tuples, conn) -> dict:
    contact_ids = [dict(r)["id"] for r in contact_tuples]
    tags = events.contact_tags
    actions: dict[str, set] = {}
    for row in conn.execute(
        "SELECT ce.contact_id, ce.type FROM campaign_events ce"
    ):
        actions.setdefault(row["contact_id"], set()).add(row["type"])
    enrollments: dict[str, set] = {}
    for row in conn.execute(
        "SELECT contact_id, sequence_id FROM sequence_enrollments"
    ):
        enrollments.setdefault(row["contact_id"], set()).add(row["sequence_id"])
    return {"tags": tags, "campaign_actions": actions, "enrollments": enrollments}


def segment_sql_filter() -> str:
    """Fast pre-filter: only contacts in subscribed-like states."""
    return "status IN ('subscribed', 'transactional')"


def evaluate(rules: list | str) -> list[dict]:
    """Return all contacts matching the ANDed rules, with tags attached."""
    if isinstance(rules, str):
        rules = json.loads(rules)
    if not rules:
        with db.get_db() as conn:
            rows = conn.execute("SELECT * FROM contacts").fetchall()
            ids = [dict(r)["id"] for r in rows]
            return [db.export_contact(conn, i) for i in ids]

    with db.get_db() as conn:
        rows = conn.execute("SELECT * FROM contacts").fetchall()
        contacts = [dict(r) for r in rows]
        ctx = _context(rows, conn)
        tag_cache: dict[str, set] = {}
        event_cache: dict[str, set] = {}
        matched = []
        for contact in contacts:
            if contact["id"] not in tag_cache:
                tag_cache[contact["id"]] = events.contact_tags(conn, contact["id"])
                event_cache[contact["id"]] = events.contact_event_types(conn, contact["id"])
            local_ctx = {
                "tags": tag_cache[contact["id"]],
                "event_types": event_cache[contact["id"]],
                "campaign_actions": ctx["campaign_actions"],
                "enrollments": ctx["enrollments"],
            }
            if all(_match(rule, contact, local_ctx) for rule in rules):
                contact["tags"] = sorted(tag_cache[contact["id"]])
                matched.append(contact)
        return matched

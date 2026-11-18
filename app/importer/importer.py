"""Import audiences, members, tags, campaigns, and engagement history from Mailchimp.

The importer is idempotent: re-running it upserts by (audience, email) for
contacts and by Mailchimp IDs for audiences/campaigns, so it can be used for a
one-time migration or periodic re-sync.
"""
import logging
from dataclasses import dataclass, field
from typing import Optional

from .. import db
from .mailchimp_client import MailchimpClient, status_to_internal

logger = logging.getLogger(__name__)


@dataclass
class ImportReport:
    audiences: int = 0
    contacts: int = 0
    tags: int = 0
    campaigns: int = 0
    events: int = 0
    errors: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "audiences_imported": self.audiences,
            "contacts_imported": self.contacts,
            "tags_imported": self.tags,
            "campaigns_imported": self.campaigns,
            "events_imported": self.events,
            "errors": self.errors,
        }


def _member_names(member: dict) -> tuple[Optional[str], Optional[str]]:
    fields = member.get("merge_fields") or {}
    first = fields.get("FNAME") or fields.get("FIRSTNAME") or member.get("first_name")
    last = fields.get("LNAME") or fields.get("LASTNAME") or member.get("last_name")
    return (first or None, last or None)


def import_audience_members(client: MailchimpClient, report: ImportReport,
                            audience_id: str, mc_list_id: str,
                            with_activity: bool = False) -> None:
    imported_tags: set = set()
    for member in client.list_members(mc_list_id):
        email = member.get("email_address")
        if not email:
            continue
        try:
            first, last = _member_names(member)
            with db.get_db() as conn:
                contact = db.upsert_contact(
                    conn,
                    audience_id=audience_id,
                    email=email,
                    first_name=first,
                    last_name=last,
                    status=_internal_status(member.get("status")),
                    mailchimp_member_id=member.get("id"),
                )
                contact_id = contact["id"]
                for tag in member.get("tags") or []:
                    tag_name = tag.get("name") if isinstance(tag, dict) else str(tag)
                    if tag_name:
                        db.attach_tag(conn, contact_id, tag_name)
                        imported_tags.add(tag_name)
            report.contacts += 1

            if with_activity:
                _import_member_activity(client, report, audience_id, mc_list_id, member)
        except Exception as exc:  # keep importing the rest of the audience
            logger.warning("Failed to import member %s: %s", email, exc)
            report.errors.append(f"member {email}: {exc}")
    report.tags += len(imported_tags)


def _internal_status(mc_status: str) -> str:
    return status_to_internal(mc_status)


def _import_member_activity(client: MailchimpClient, report: ImportReport,
                            audience_id: str, mc_list_id: str, member: dict) -> None:
    """Import recent opens/clicks for one member and attach them to matching campaigns."""
    member_id = member.get("id")
    if not member_id:
        return
    with db.get_db() as conn:
        contact = conn.execute(
            "SELECT id FROM contacts WHERE mailchimp_member_id = ?", (member_id,)
        ).fetchone()
        if not contact:
            return
        contact_id = contact["id"]
        for activity in client.member_activity(mc_list_id, member_id):
            action = activity.get("action")
            if action not in ("open", "click"):
                continue
            campaign_id_resolved = _resolve_campaign(conn, activity.get("campaign_id"))
            if not campaign_id_resolved:
                continue
            ts = _parse_mailchimp_time(activity.get("timestamp"))
            existing = conn.execute(
                """SELECT 1 FROM campaign_events
                   WHERE campaign_id = ? AND contact_id = ? AND type = ? AND timestamp = ?""",
                (campaign_id_resolved, contact_id, action, ts),
            ).fetchone()
            if existing:
                continue
            conn.execute(
                """INSERT INTO campaign_events (id, campaign_id, contact_id, type, timestamp)
                   VALUES (?, ?, ?, ?, ?)""",
                (db.new_id(), campaign_id_resolved, contact_id, action, ts),
            )
            report.events += 1


def _resolve_campaign(conn, mc_campaign_id) -> Optional[str]:
    if not mc_campaign_id:
        return None
    row = conn.execute(
        "SELECT id FROM campaigns WHERE mailchimp_campaign_id = ?", (mc_campaign_id,)
    ).fetchone()
    return row["id"] if row else None


def _parse_mailchimp_time(value) -> float:
    if not value:
        return 0.0
    from datetime import datetime, timezone
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def import_campaigns(client: MailchimpClient, report: ImportReport,
                     default_audience_id: Optional[str] = None) -> None:
    for campaign in client.list_campaigns():
        try:
            mc_campaign_id = campaign["id"]
            mc_list_id = campaign.get("list_id")
            with db.get_db() as conn:
                audience_row = None
                if mc_list_id:
                    audience_row = conn.execute(
                        "SELECT id FROM audiences WHERE mailchimp_list_id = ?", (mc_list_id,)
                    ).fetchone()
                audience_id = audience_row["id"] if audience_row else default_audience_id
                if not audience_id:
                    report.errors.append(
                        f"campaign {mc_campaign_id}: no matching audience for list {mc_list_id}"
                    )
                    continue
                settings = campaign.get("settings") or {}
                content = client.campaign_content(mc_campaign_id)
                html = (
                    content.get("html")
                    or content.get("template", {}).get("html")
                    or "<p>(no HTML content available)</p>"
                )
                sent_at = _parse_mailchimp_time(campaign.get("send_time")) or None
                conn.execute(
                    """INSERT INTO campaigns
                         (id, audience_id, name, subject, preheader, from_name, from_email,
                          html_content, status, mailchimp_campaign_id, sent_at, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(mailchimp_campaign_id) DO UPDATE SET
                         name = excluded.name,
                         subject = excluded.subject,
                         html_content = excluded.html_content,
                         status = excluded.status,
                         sent_at = COALESCE(excluded.sent_at, campaigns.sent_at)""",
                    (
                        db.new_id(),
                        audience_id,
                        (settings.get("title") or "Imported campaign")[:200],
                        (settings.get("subject_line") or "(no subject)")[:300],
                        settings.get("preview_text"),
                        settings.get("from_name"),
                        campaign.get("from_email") or settings.get("reply_to"),
                        html,
                        "sent" if sent_at else "draft",
                        mc_campaign_id,
                        sent_at,
                        db.now(),
                    ),
                )
                report.campaigns += 1
        except Exception as exc:
            logger.warning("Failed to import campaign %s: %s", campaign.get("id"), exc)
            report.errors.append(f"campaign {campaign.get('id')}: {exc}")


def import_all(api_key: str, with_activity: bool = False,
               base_url: Optional[str] = None) -> dict:
    """Full import: audiences -> members + tags -> campaigns -> engagement history."""
    report = ImportReport()
    client = MailchimpClient(api_key, base_url=base_url)
    try:
        for audience in client.list_audiences():
            mc_list_id = audience["id"]
            try:
                with db.get_db() as conn:
                    existing = conn.execute(
                        "SELECT id FROM audiences WHERE mailchimp_list_id = ?", (mc_list_id,)
                    ).fetchone()
                    if existing:
                        audience_id = existing["id"]
                        conn.execute(
                            "UPDATE audiences SET name = ? WHERE id = ?",
                            (audience.get("name") or audience_id[:12], audience_id),
                        )
                    else:
                        audience_id = db.new_id()
                        conn.execute(
                            """INSERT INTO audiences (id, name, mailchimp_list_id, created_at)
                               VALUES (?, ?, ?, ?)""",
                            (audience_id, audience.get("name") or "Imported audience",
                             mc_list_id, db.now()),
                        )
                report.audiences += 1
                import_audience_members(
                    client, report, audience_id, mc_list_id, with_activity=with_activity
                )
            except Exception as exc:
                logger.warning("Failed to import audience %s: %s", mc_list_id, exc)
                report.errors.append(f"audience {mc_list_id}: {exc}")
        if report.audiences or report.contacts:
            with db.get_db() as conn:
                first_audience = conn.execute(
                    "SELECT id FROM audiences WHERE mailchimp_list_id IS NOT NULL LIMIT 1"
                ).fetchone()
                import_campaigns(
                    client, report, default_audience_id=first_audience["id"] if first_audience else None
                )
    finally:
        client.close()
    return report.to_dict()

"""SQLite storage layer for the newsletter service."""
import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager

DB_PATH = os.environ.get("NEWSLETTER_DB", os.path.join(os.path.dirname(__file__), "..", "data", "newsletter.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS audiences (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    mailchimp_list_id TEXT UNIQUE,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS contacts (
    id TEXT PRIMARY KEY,
    audience_id TEXT NOT NULL REFERENCES audiences(id),
    email TEXT NOT NULL,
    first_name TEXT,
    last_name TEXT,
    status TEXT NOT NULL DEFAULT 'subscribed',
    signup_source TEXT,
    mailchimp_member_id TEXT UNIQUE,
    unsubscribed_at REAL,
    created_at REAL NOT NULL,
    UNIQUE (audience_id, email)
);

CREATE TABLE IF NOT EXISTS tags (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    UNIQUE (name)
);

CREATE TABLE IF NOT EXISTS contact_tags (
    contact_id TEXT NOT NULL REFERENCES contacts(id),
    tag_id TEXT NOT NULL REFERENCES tags(id),
    PRIMARY KEY (contact_id, tag_id)
);

CREATE TABLE IF NOT EXISTS campaigns (
    id TEXT PRIMARY KEY,
    audience_id TEXT REFERENCES audiences(id),
    name TEXT NOT NULL,
    subject TEXT NOT NULL,
    preheader TEXT,
    from_name TEXT,
    from_email TEXT,
    html_content TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',
    mailchimp_campaign_id TEXT UNIQUE,
    scheduled_at REAL,
    sent_at REAL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS campaign_events (
    id TEXT PRIMARY KEY,
    campaign_id TEXT NOT NULL REFERENCES campaigns(id),
    contact_id TEXT NOT NULL REFERENCES contacts(id),
    type TEXT NOT NULL,
    timestamp REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS templates (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    subject TEXT NOT NULL DEFAULT '',
    html_content TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS sequences (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    audience_id TEXT REFERENCES audiences(id),
    trigger_type TEXT NOT NULL DEFAULT 'manual',
    trigger_value TEXT,
    status TEXT NOT NULL DEFAULT 'draft',
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS sequence_steps (
    id TEXT PRIMARY KEY,
    sequence_id TEXT NOT NULL REFERENCES sequences(id),
    position INTEGER NOT NULL,
    delay_hours REAL NOT NULL DEFAULT 0,
    subject TEXT NOT NULL,
    html_content TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sequence_enrollments (
    id TEXT PRIMARY KEY,
    sequence_id TEXT NOT NULL REFERENCES sequences(id),
    contact_id TEXT NOT NULL REFERENCES contacts(id),
    next_step INTEGER NOT NULL DEFAULT 0,
    next_send_at REAL,
    status TEXT NOT NULL DEFAULT 'active',
    created_at REAL NOT NULL,
    UNIQUE (sequence_id, contact_id)
);

CREATE TABLE IF NOT EXISTS subscriber_events (
    id TEXT PRIMARY KEY,
    contact_id TEXT,
    email TEXT,
    type TEXT NOT NULL,
    source TEXT,
    value TEXT,
    timestamp REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sub_events_contact ON subscriber_events(contact_id);
CREATE INDEX IF NOT EXISTS idx_sub_events_type ON subscriber_events(type);

CREATE TABLE IF NOT EXISTS segments (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    rules TEXT NOT NULL DEFAULT '[]',
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS posts (
    id TEXT PRIMARY KEY,
    slug TEXT UNIQUE NOT NULL,
    title TEXT NOT NULL,
    subtitle TEXT,
    html_content TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'draft',
    visibility TEXT NOT NULL DEFAULT 'public',
    published_at REAL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS products (
    id TEXT PRIMARY KEY,
    slug TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    price_cents INTEGER NOT NULL DEFAULT 0,
    product_type TEXT NOT NULL DEFAULT 'download',
    file_url TEXT,
    published INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    id TEXT PRIMARY KEY,
    product_id TEXT NOT NULL REFERENCES products(id),
    email TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'paid',
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS subscription_plans (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    price_cents INTEGER NOT NULL,
    interval TEXT NOT NULL DEFAULT 'monthly',
    published INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS subscriptions (
    id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL REFERENCES subscription_plans(id),
    email TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    provider TEXT NOT NULL DEFAULT 'mock',
    started_at REAL NOT NULL,
    canceled_at REAL
);

CREATE TABLE IF NOT EXISTS services (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    duration_min INTEGER NOT NULL DEFAULT 30,
    price_cents INTEGER NOT NULL DEFAULT 0,
    availability TEXT NOT NULL DEFAULT '',
    published INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS bookings (
    id TEXT PRIMARY KEY,
    service_id TEXT NOT NULL REFERENCES services(id),
    email TEXT NOT NULL,
    name TEXT,
    slot_ts REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'booked',
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS pages (
    id TEXT PRIMARY KEY,
    slug TEXT UNIQUE NOT NULL,
    title TEXT NOT NULL,
    html_content TEXT NOT NULL DEFAULT '',
    page_type TEXT NOT NULL DEFAULT 'landing',
    audience_id TEXT REFERENCES audiences(id),
    published INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS page_variants (
    id TEXT PRIMARY KEY,
    page_id TEXT NOT NULL REFERENCES pages(id),
    label TEXT NOT NULL,
    html_content TEXT NOT NULL,
    weight REAL NOT NULL DEFAULT 1.0
);

CREATE TABLE IF NOT EXISTS ab_tests (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    campaign_id TEXT REFERENCES campaigns(id),
    object_type TEXT NOT NULL DEFAULT 'email',
    metric TEXT NOT NULL DEFAULT 'open_rate',
    status TEXT NOT NULL DEFAULT 'running',
    winner_label TEXT,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS ab_variants (
    id TEXT PRIMARY KEY,
    test_id TEXT NOT NULL REFERENCES ab_tests(id),
    label TEXT NOT NULL,
    subject TEXT NOT NULL,
    html_content TEXT,
    recipients INTEGER NOT NULL DEFAULT 0,
    opens INTEGER NOT NULL DEFAULT 0,
    clicks INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS referrals (
    id TEXT PRIMARY KEY,
    referrer_contact_id TEXT NOT NULL REFERENCES contacts(id),
    referred_email TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS creator_recommendations (
    id TEXT PRIMARY KEY,
    owner_newsletter TEXT NOT NULL DEFAULT '',
    partner_name TEXT NOT NULL,
    partner_url TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1,
    clicks INTEGER NOT NULL DEFAULT 0,
    impressions INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS sponsorships (
    id TEXT PRIMARY KEY,
    sponsor_name TEXT NOT NULL,
    sponsor_url TEXT,
    ad_copy TEXT NOT NULL DEFAULT '',
    price_cents INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'available',
    campaign_id TEXT,
    booked_by_email TEXT,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS webhooks (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    url TEXT NOT NULL,
    events TEXT NOT NULL DEFAULT '[]',
    secret TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS forms (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    audience_id TEXT REFERENCES audiences(id),
    headline TEXT NOT NULL DEFAULT 'Subscribe',
    button_text TEXT NOT NULL DEFAULT 'Subscribe',
    thank_you_message TEXT NOT NULL DEFAULT 'Thanks! Check your inbox.',
    tags TEXT NOT NULL DEFAULT '[]',
    source TEXT NOT NULL DEFAULT 'form',
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_contacts_audience ON contacts(audience_id);
CREATE INDEX IF NOT EXISTS idx_contacts_email ON contacts(email);
CREATE INDEX IF NOT EXISTS idx_events_campaign ON campaign_events(campaign_id);
"""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def get_db():
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    db_dir = os.path.dirname(DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    with get_db() as conn:
        conn.executescript(SCHEMA)


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def now() -> float:
    return time.time()


def row_to_dict(row: sqlite3.Row) -> dict:
    return {key: row[key] for key in row.keys()}


def get_setting(conn: sqlite3.Connection, key: str, default=None):
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def upsert_contact(conn: sqlite3.Connection, audience_id: str, email: str,
                   first_name=None, last_name=None, status="subscribed",
                   mailchimp_member_id=None, signup_source=None) -> dict:
    """Insert or update a contact by (audience_id, email). Returns the contact row dict."""
    email = email.strip().lower()
    existing = conn.execute(
        "SELECT id FROM contacts WHERE audience_id = ? AND email = ?",
        (audience_id, email),
    ).fetchone()
    if existing:
        contact_id = existing["id"]
        conn.execute(
            """UPDATE contacts SET
                 first_name = COALESCE(?, first_name),
                 last_name = COALESCE(?, last_name),
                 status = ?,
                 mailchimp_member_id = COALESCE(?, mailchimp_member_id)
               WHERE id = ?""",
            (first_name, last_name, status, mailchimp_member_id, contact_id),
        )
    else:
        contact_id = new_id()
        conn.execute(
            """INSERT INTO contacts
                 (id, audience_id, email, first_name, last_name, status, mailchimp_member_id,
                  signup_source, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (contact_id, audience_id, email, first_name, last_name, status,
             mailchimp_member_id, signup_source, now()),
        )
    return row_to_dict(conn.execute("SELECT * FROM contacts WHERE id = ?", (contact_id,)).fetchone())


def attach_tag(conn: sqlite3.Connection, contact_id: str, tag_name: str) -> None:
    tag_name = tag_name.strip()
    if not tag_name:
        return
    row = conn.execute("SELECT id FROM tags WHERE name = ?", (tag_name,)).fetchone()
    if row:
        tag_id = row["id"]
    else:
        tag_id = new_id()
        conn.execute("INSERT INTO tags (id, name) VALUES (?, ?)", (tag_id, tag_name))
    conn.execute(
        "INSERT OR IGNORE INTO contact_tags (contact_id, tag_id) VALUES (?, ?)",
        (contact_id, tag_id),
    )


def export_contact(conn: sqlite3.Connection, contact_id: str) -> dict:
    """Full contact representation including tags."""
    row = conn.execute("SELECT * FROM contacts WHERE id = ?", (contact_id,)).fetchone()
    contact = row_to_dict(row)
    contact["tags"] = [
        r["name"]
        for r in conn.execute(
            """SELECT t.name FROM tags t
               JOIN contact_tags ct ON ct.tag_id = t.id
               WHERE ct.contact_id = ? ORDER BY t.name""",
            (contact_id,),
        )
    ]
    return contact

"""Commerce: digital products, orders, paid subscriptions, and bookings."""
import calendar
import datetime as dt
import json

from . import db
from .events import record_event


def slim_html(page_title: str, body: str, base: str = "") -> str:
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{page_title}</title>
<style>
 body {{ font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif; background:#f6f7fb;
        color:#1c2333; margin:0; }}
 .container {{ max-width:720px; margin:40px auto 80px; padding:0 20px; }}
 header a {{ color:#4f46e5; text-decoration:none; font-weight:600; }}
 .card {{ background:#fff; border:1px solid #e3e6ef; border-radius:14px; padding:28px; }}
 h1 {{ margin-top:0; }} .muted {{ color:#68718a; font-size:14px; }}
 .btn {{ display:inline-block; background:#4f46e5; color:#fff; padding:10px 18px;
        border-radius:8px; text-decoration:none; font-weight:600; }}
 .price {{ font-size:20px; font-weight:700; }}
 input, button {{ padding:9px 12px; border-radius:8px; border:1px solid #e3e6ef; font-size:14px; }}
 button {{ background:#4f46e5; color:#fff; border:none; font-weight:600; cursor:pointer; }}
 form {{ display:flex; gap:8px; margin-top:14px; }} form input {{ flex:1; }}
</style></head>
<body><div class="container">{body}</div></body></html>"""


def create_product(conn, name: str, price_cents: int, product_type: str = "download",
                   description: str = "", file_url: str | None = None,
                   published: bool = False) -> dict:
    slug = _slugify(name)
    existing = conn.execute("SELECT slug FROM products WHERE slug = ?", (slug,)).fetchone()
    if existing:
        slug = f"{slug}-{db.new_id()[:4]}"
    product_id = db.new_id()
    conn.execute(
        """INSERT INTO products (id, slug, name, description, price_cents, product_type,
                                 file_url, published, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (product_id, slug, name, description, price_cents, product_type, file_url,
         int(published), db.now()),
    )
    return _get_product(conn, product_id)


def _get_product(conn, product_id: str) -> dict:
    return db.row_to_dict(conn.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone())


def _slugify(text: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in text.lower()).strip("-")[:60] or "item"


def create_order(conn, product_id: str, email: str, status: str = "paid") -> dict:
    product = _get_product(conn, product_id)
    order_id = db.new_id()
    conn.execute(
        """INSERT INTO orders (id, product_id, email, amount_cents, status, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (order_id, product_id, email.lower(), product["price_cents"], status, db.now()),
    )
    contact = conn.execute(
        "SELECT id FROM contacts WHERE email = ?", (email.lower(),)
    ).fetchone()
    record_event(conn, contact["id"] if contact else None, "purchase",
                 email=email, value=product_id)
    return db.row_to_dict(conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone())


def create_plan(conn, name: str, price_cents: int, interval: str = "monthly",
                description: str = "", published: bool = False) -> dict:
    plan_id = db.new_id()
    conn.execute(
        """INSERT INTO subscription_plans (id, name, description, price_cents, interval,
                                           published, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (plan_id, name, description, price_cents, interval, int(published), db.now()),
    )
    return db.row_to_dict(conn.execute("SELECT * FROM subscription_plans WHERE id = ?", (plan_id,)).fetchone())


def create_subscription(conn, plan_id: str, email: str) -> dict:
    plan = conn.execute("SELECT * FROM subscription_plans WHERE id = ?", (plan_id,)).fetchone()
    if not plan:
        raise ValueError("Plan not found")
    sub_id = db.new_id()
    conn.execute(
        """INSERT INTO subscriptions (id, plan_id, email, status, provider, started_at)
           VALUES (?, ?, ?, 'active', 'mock', ?)""",
        (sub_id, plan_id, email.lower(), db.now()),
    )
    contact = conn.execute("SELECT id FROM contacts WHERE email = ?", (email.lower(),)).fetchone()
    record_event(conn, contact["id"] if contact else None, "subscription_started",
                 email=email, value=plan_id)
    return db.row_to_dict(conn.execute("SELECT * FROM subscriptions WHERE id = ?", (sub_id,)).fetchone())


def cancel_subscription(conn, subscription_id: str) -> None:
    conn.execute(
        "UPDATE subscriptions SET status = 'canceled', canceled_at = ? WHERE id = ?",
        (db.now(), subscription_id),
    )


def parse_availability(spec: str) -> dict:
    """Availability JSON like {"Mon": ["09:00-12:00", "14:00-17:00"]} (case-insensitive keys)."""
    if not spec:
        return {}
    try:
        data = json.loads(spec)
    except json.JSONDecodeError:
        return {}
    return {k.capitalize(): v for k, v in data.items()}


DAY_KEYS = {0: "Mon", 1: "Tue", 2: "Wed", 3: "Thu", 4: "Fri", 5: "Sat", 6: "Sun"}


def available_slots(conn, service_id: str, days: int = 14) -> list[float]:
    """Generate bookable timestamps: day windows crossed with the service duration."""
    service_row = conn.execute("SELECT * FROM services WHERE id = ?", (service_id,)).fetchone()
    if not service_row:
        return []
    availability = parse_availability(service_row["availability"])
    booked = {
        r["slot_ts"]
        for r in conn.execute(
            "SELECT slot_ts FROM bookings WHERE service_id = ? AND status = 'booked'",
            (service_id,),
        )
    }
    slots: list[float] = []
    today = dt.date.today()
    for offset in range(days):
        day = today + dt.timedelta(days=offset)
        windows = availability.get(DAY_KEYS[day.weekday()], [])
        for window in windows:
            start_s, end_s = window.split("-")
            start = dt.datetime.combine(day, dt.time.fromisoformat(start_s))
            end = dt.datetime.combine(day, dt.time.fromisoformat(end_s))
            step = dt.timedelta(minutes=service_row["duration_min"])
            cursor = start
            while cursor + step <= end:
                ts = calendar.timegm(cursor.timetuple())
                if ts > db.now() and ts not in booked:
                    slots.append(ts)
                cursor += step
    return slots


def book_slot(conn, service_id: str, email: str, slot_ts: float,
              name: str | None = None) -> dict:
    if slot_ts not in available_slots(conn, service_id):
        raise ValueError("Slot no longer available")
    booking_id = db.new_id()
    conn.execute(
        """INSERT INTO bookings (id, service_id, email, name, slot_ts, status, created_at)
           VALUES (?, ?, ?, ?, ?, 'booked', ?)""",
        (booking_id, service_id, email.lower(), name, slot_ts, db.now()),
    )
    contact = conn.execute("SELECT id FROM contacts WHERE email = ?", (email.lower(),)).fetchone()
    record_event(conn, contact["id"] if contact else None, "booking_confirmed",
                 email=email, value=service_id)
    return db.row_to_dict(conn.execute("SELECT * FROM bookings WHERE id = ?", (booking_id,)).fetchone())

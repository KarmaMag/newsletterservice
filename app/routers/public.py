"""Public-facing site: blog/newsletter archive, landing pages with A/B variants,
shop, paid plans, booking pages, forms, referral links, creator-network clicks."""
import datetime as dt
import urllib.parse

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import ab, commerce, db
from ..events import record_event, record_event_now
from ..webhooks import dispatch_async

router = APIRouter()


# ---------- subscription machinery --------------------------------------------------

def subscribe_email(conn, audience_id: str, email: str, source: str | None = None,
                    tags: list[str] | None = None, referred_by: str | None = None) -> dict:
    email = email.strip().lower()
    contact = db.upsert_contact(conn, audience_id, email,
                                status="subscribed", signup_source=source)
    contact_id = contact["id"]
    for tag in tags or []:
        db.attach_tag(conn, contact_id, tag)
    if referred_by:
        referrer = conn.execute("SELECT id FROM contacts WHERE id = ?", (referred_by,)).fetchone()
        if referrer:
            conn.execute(
                """INSERT INTO referrals (id, referrer_contact_id, referred_email, created_at)
                   VALUES (?, ?, ?, ?)""",
                (db.new_id(), referrer["id"], email, db.now()),
            )
            db.attach_tag(conn, referrer["id"], "referral-source")
    record_event(conn, contact_id, "form_signup", email=email, source=source)
    from ..automations import enroll_by_trigger
    enroll_by_trigger(conn, "signup", source, contact_id, audience_id)
    return contact




async def _form_dict(request: Request) -> dict:
    """Parse x-www-form-urlencoded bodies (browser forms) without extra deps."""
    body = (await request.body()).decode("utf-8", errors="replace")
    if not body:
        return {}
    return {k: v[0] for k, v in urllib.parse.parse_qs(body).items()}


def _esc(s) -> str:
    import html

    return html.escape(str(s or ""))


def _page(title: str, body: str) -> HTMLResponse:
    import os

    base = os.environ.get("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")
    brand = os.environ.get("BRAND_NAME", "Newsletter")
    return HTMLResponse(_page_html(title, body, base, brand))


def _page_html(title: str, body: str, base: str, brand: str) -> str:
    nav = (f'<header style="display:flex;align-items:center;padding:18px 28px;background:#fff;'
           f'border-bottom:1px solid #e3e6ef"><a href="{base}/blog" '
           f'style="color:#4f46e5;font-weight:700;text-decoration:none;font-size:17px">{_esc(brand)}</a>'
           f'<nav style="margin-left:auto;display:flex;gap:16px;font-size:14px">'
           f'<a href="{base}/blog">Blog</a><a href="{base}/shop">Shop</a>'
           f'<a href="{base}/plans">Membership</a><a href="{base}/book">Bookings</a></nav></header>')
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(title)} · {_esc(brand)}</title>
<style>
 body {{ font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
        background:#f6f7fb; color:#1c2333; margin:0; }}
 .container {{ max-width:760px; margin:32px auto 80px; padding:0 20px; }}
 .card {{ background:#fff; border:1px solid #e3e6ef; border-radius:14px; padding:28px; margin-bottom:18px; }}
 h1 {{ margin-top:0; }} h3 {{ margin:18px 0 6px; }}
 .muted {{ color:#68718a; font-size:14px; }}
 .btn {{ display:inline-block; background:#4f46e5; color:#fff; padding:9px 16px;
        border-radius:8px; text-decoration:none; font-weight:600; border:none; cursor:pointer; font-size:14px; }}
 form {{ display:flex; gap:8px; margin-top:14px; }}
 input {{ flex:1; padding:9px 12px; border:1px solid #e3e6ef; border-radius:8px; font-size:14px; }}
 .price {{ font-size:18px; font-weight:700; }}
</style></head>
<body>{nav}<div class="container">{body}</div></body></html>"""


# ---------- blog / newsletter publication ------------------------------------------

@router.get("/blog", response_class=HTMLResponse)
def blog_index():
    with db.get_db() as conn:
        posts = conn.execute(
            "SELECT * FROM posts WHERE status = 'published' ORDER BY published_at DESC"
        ).fetchall()
    items = "".join(
        f'<div class="card"><h3 style="margin-top:0">{_esc(p["title"])}</h3>'
        f'<p class="muted">{_esc(p["subtitle"] or "")}</p>'
        f'{"<span class=muted>Paid members only</span><br><br>" if p["visibility"] == "paid" else ""}'
        f'<a class="btn" href="/blog/{_esc(p["slug"])}">Read post</a></div>'
        for p in posts
    ) or '<div class="card"><p class="muted">No posts published yet.</p></div>'
    return _page("Blog", f"<h1>Blog &amp; Newsletter</h1>{items}")


@router.get("/blog/{slug}", response_class=HTMLResponse)
def blog_post(slug: str):
    with db.get_db() as conn:
        post = conn.execute("SELECT * FROM posts WHERE slug = ? AND status = 'published'", (slug,)).fetchone()
        if not post:
            raise HTTPException(404, "Post not found")
        content = post["html_content"]
        if post["visibility"] == "paid":
            content = (content[:400] + "…"
                       + '<div class="card" style="margin-top:18px"><h3>This is a paid post</h3>'
                         '<p class="muted">Become a paid member to keep reading.</p>'
                         '<a class="btn" href="/plans">See membership plans</a></div>')
    record_event_now(None, "post_view", source=post["id"])
    return _page(post["title"], f'<h1>{_esc(post["title"])}</h1>'
                 f'<p class="muted">{_esc(post["subtitle"] or "")}</p>{content}')


# ---------- landing pages (with A/B variants) ----------------------------------------

@router.get("/p/{slug}", response_class=HTMLResponse)
def landing_page(slug: str):
    with db.get_db() as conn:
        page = conn.execute("SELECT * FROM pages WHERE slug = ?", (slug,)).fetchone()
        if not page or not page["published"]:
            raise HTTPException(404, "Page not found")
        variant = ab.page_variant_for(conn, page["id"], None)
        body_html = variant["html_content"] if variant else page["html_content"]
        view_source = f"page:{page['id']}:{variant['label']}" if variant else f"page:{page['id']}"
    record_event_now(None, "page_view", source=view_source)
    return _page(page["title"], f"""
        {body_html}
        <form method="post" action="/p/{_esc(slug)}/subscribe">
          <input type="email" name="email" placeholder="you@example.com" required>
          <button class="btn" type="submit">Subscribe</button>
        </form>""")


@router.post("/p/{slug}/subscribe", response_class=HTMLResponse)
async def landing_subscribe(slug: str, request: Request):
    data = await _form_dict(request)
    email, ref = data.get("email", ""), data.get("ref", "")
    with db.get_db() as conn:
        page = conn.execute("SELECT * FROM pages WHERE slug = ?", (slug,)).fetchone()
        if not page:
            raise HTTPException(404, "Page not found")
        if not page["audience_id"]:
            raise HTTPException(400, "Page has no audience configured")
        contact = subscribe_email(conn, page["audience_id"], email,
                                  source=f"page:{page['id']}", referred_by=ref or None)
        variant = ab.page_variant_for(conn, page["id"], email)
        if variant:
            record_event(conn, None, "page_conversion", email=email,
                         source=f"page:{page['id']}:{variant['label']}")
    dispatch_async("form_signup", {"email": email, "page": slug})
    return _page(page["title"], f'<h1>Thanks!</h1><p class="muted">{_esc(email)} is on the list.</p>')


# ---------- forms ----------------------------------------------------------------------

@router.get("/f/{form_id}", response_class=HTMLResponse)
def form_page(form_id: str):
    with db.get_db() as conn:
        form = conn.execute("SELECT * FROM forms WHERE id = ?", (form_id,)).fetchone()
        if not form:
            raise HTTPException(404, "Form not found")
    return _page(form["name"], f"""
        <div class="card"><h2>{_esc(form["headline"])}</h2>
        <form method="post" action="/f/{_esc(form_id)}/submit">
          <input type="email" name="email" placeholder="you@example.com" required>
          <button class="btn" type="submit">{_esc(form["button_text"])}</button>
        </form></div>""")


@router.post("/f/{form_id}/submit", response_class=HTMLResponse)
@router.post("/f/{form_id}/subscribe", response_class=HTMLResponse)
async def form_submit(form_id: str, request: Request):
    import json as _json

    data = await _form_dict(request)
    email, ref = data.get("email", ""), data.get("ref", "")
    with db.get_db() as conn:
        form = conn.execute("SELECT * FROM forms WHERE id = ?", (form_id,)).fetchone()
        if not form:
            raise HTTPException(404, "Form not found")
        contact = subscribe_email(conn, form["audience_id"], email,
                                  source=f"form:{form_id}",
                                  tags=_json.loads(form["tags"] or "[]"),
                                  referred_by=ref or None)
    dispatch_async("form_signup", {"email": email, "form": form_id})
    return _page(form["name"], f'<h1>{_esc(form["thank_you_message"])}</h1>'
                 f'<p class="muted">{_esc(email)} is on the list.</p>')


# ---------- shop -------------------------------------------------------------------------

@router.get("/shop", response_class=HTMLResponse)
def shop():
    with db.get_db() as conn:
        products = conn.execute("SELECT * FROM products WHERE published = 1 ORDER BY created_at DESC").fetchall()
    items = "".join(
        f'<div class="card"><h3 style="margin-top:0">{_esc(p["name"])} '
        f'<span class="muted">({_esc(p["product_type"])})</span></h3>'
        f'<p class="muted">{_esc(p["description"])}</p>'
        f'<p class="price">{"$" + format(p["price_cents"] / 100, ",.2f") if p["price_cents"] else "Free"}</p>'
        f'<a class="btn" href="/shop/{_esc(p["slug"])}">Get it</a></div>'
        for p in products
    ) or '<div class="card"><p class="muted">Shop is empty.</p></div>'
    return _page("Shop", f"<h1>Shop digital products</h1>{items}")


@router.get("/shop/{slug}", response_class=HTMLResponse)
def product_page(slug: str):
    with db.get_db() as conn:
        product = conn.execute("SELECT * FROM products WHERE slug = ? AND published = 1", (slug,)).fetchone()
        if not product:
            raise HTTPException(404, "Product not found")
    price = product["price_cents"] / 100
    record_event_now(None, "product_view", source=product["id"])
    return _page(product["name"], f"""
      <div class="card">
        <h1>{_esc(product['name'])}</h1>
        <p class="muted">{_esc(product['description'])}</p>
        <p class="price">{'$' + format(price, ',.2f') if price else 'Free'}</p>
        <form method="post" action="/shop/{_esc(slug)}/buy">
          <input type="email" name="email" placeholder="you@example.com" required>
          <button class="btn" type="submit">{'Buy — demo checkout' if price else 'Get free copy'}</button>
        </form>
        <p class="muted">Demo checkout: the order is recorded instantly.</p>
      </div>""")


@router.post("/shop/{slug}/buy", response_class=HTMLResponse)
async def buy_product(slug: str, request: Request):
    email = (await _form_dict(request)).get("email", "")
    with db.get_db() as conn:
        product = conn.execute("SELECT * FROM products WHERE slug = ? AND published = 1", (slug,)).fetchone()
        if not product:
            raise HTTPException(404, "Product not found")
        order = commerce.create_order(conn, product["id"], email, status="paid")
        contact = db.upsert_contact(conn, _default_audience(conn), email,
                                    signup_source=f"purchase:{product['slug']}")
        db.attach_tag(conn, contact["id"], f"customer")
        dispatch_async("purchase", {"email": email, "product": product["name"],
                                    "amount_cents": order["amount_cents"]})
    link = product["file_url"] or "#"
    return _page(product["name"],
                 f"<h1>Thank you for your purchase!</h1>"
                 f"<p class='muted'>Order <b>{order['id']}</b> confirmed. A receipt would be emailed to {_esc(email)}.</p>"
                 f'<a class="btn" href="{_esc(link)}">Download / access</a>')


def _default_audience(conn) -> str:
    row = conn.execute("SELECT id FROM audiences ORDER BY created_at LIMIT 1").fetchone()
    if row:
        return row["id"]
    audience_id = db.new_id()
    conn.execute("INSERT INTO audiences (id, name, created_at) VALUES (?, 'Customers', ?)",
                 (audience_id, db.now()))
    return audience_id


# ---------- paid subscriptions -------------------------------------------------------------

@router.get("/plans", response_class=HTMLResponse)
def plans():
    with db.get_db() as conn:
        rows = conn.execute("SELECT * FROM subscription_plans WHERE published = 1 ORDER BY price_cents").fetchall()
    cards = "".join(
        f'<div class="card"><h3 style="margin-top:0">{_esc(p["name"])}</h3>'
        f'<p class="muted">{_esc(p["description"])}</p>'
        f'<p class="price">{"$" + format(p["price_cents"] / 100, ",.2f")}/{_esc(p["interval"])}</p>'
        f'''<form method="post" action="/plans/{_esc(p["id"])}/subscribe">
              <input type="email" name="email" placeholder="you@example.com" required>
              <button class="btn" type="submit">Join — demo checkout</button>
            </form></div>'''
        for p in rows
    ) or '<div class="card"><p class="muted">No plans published yet.</p></div>'
    return _page("Membership", f"<h1>Paid membership</h1>{cards}")


@router.post("/plans/{plan_id}/subscribe", response_class=HTMLResponse)
async def subscribe_plan_page(plan_id: str, request: Request):
    email = (await _form_dict(request)).get("email", "")
    with db.get_db() as conn:
        try:
            sub = commerce.create_subscription(conn, plan_id, email)
        except ValueError:
            raise HTTPException(404, "Plan not found")
        plan = conn.execute("SELECT name FROM subscription_plans WHERE id = ?", (plan_id,)).fetchone()
        db.upsert_contact(conn, _default_audience(conn), email, signup_source="paid_subscription")
        db.attach_tag(conn, conn.execute("SELECT id FROM contacts WHERE email = ?", (email.lower(),)).fetchone()["id"],
                      "paid-member")
        dispatch_async("subscription_started", {"email": email, "plan": plan["name"]})
    return _page("Membership", f"<h1>Welcome to the paid tier!</h1>"
                 f"<p class='muted'>Subscription <b>{sub['id']}</b> is active (demo billing)."
                 f" Receipts would be emailed to {_esc(email)}.</p>")


# ---------- bookings -------------------------------------------------------------------------

@router.get("/book", response_class=HTMLResponse)
def services_index():
    with db.get_db() as conn:
        rows = conn.execute("SELECT * FROM services WHERE published = 1 ORDER BY created_at").fetchall()
    items = "".join(
        f"<div class='card'><h3 style='margin-top:0'>{_esc(s['name'])}</h3>"
        f"<p class='muted'>{_esc(s['description'])}</p>"
        f"<p class='muted'>{s['duration_min']} min · "
        f"{'$' + format(s['price_cents'] / 100, ',.2f') if s['price_cents'] else 'Free'}</p>"
        f"<a class='btn' href='/book/{_esc(s['id'])}'>Pick a time</a></div>"
        for s in rows
    ) or "<div class='card'><p class='muted'>No services published.</p></div>"
    return _page("Book", f"<h1>Book a session</h1>{items}")


@router.get("/book/{service_id}", response_class=HTMLResponse)
def booking_page(service_id: str):
    with db.get_db() as conn:
        service = conn.execute("SELECT * FROM services WHERE id = ?", (service_id,)).fetchone()
        if not service:
            raise HTTPException(404, "Service not found")
        slots = commerce.available_slots(conn, service_id, days=14)
    if not slots:
        return _page(service["name"], "<h1>No open slots</h1><p class='muted'>Check back soon.</p>")
    slot_items = "".join(
        f"""<form method="post" action="/book/{_esc(service_id)}/book" style="flex-direction:column;align-items:stretch">
              <b>{dt.datetime.utcfromtimestamp(s).strftime('%a %d %b, %H:%M UTC')}</b>
              <input type="hidden" name="slot_ts" value="{s}">
              <input type="email" name="email" placeholder="you@example.com" required>
              <input type="text" name="name" placeholder="Your name">
              <button class="btn" type="submit">Book this slot</button>
            </form><hr class="muted">"""
        for s in slots[:12]
    )
    price = service["price_cents"] / 100
    return _page(service["name"], f"<h1>{_esc(service['name'])}</h1>"
                 f"<p class='muted'>{_esc(service['description'])} · "
                 f"{service['duration_min']} min · {'$' + format(price, ',.2f') if price else 'Free'}</p>"
                 f"{slot_items}")


@router.post("/book/{service_id}/book", response_class=HTMLResponse)
async def confirm_booking(service_id: str, request: Request):
    data = await _form_dict(request)
    email, slot_ts, name = data.get("email", ""), data.get("slot_ts", ""), data.get("name", "")
    with db.get_db() as conn:
        service = conn.execute("SELECT * FROM services WHERE id = ?", (service_id,)).fetchone()
        if not service:
            raise HTTPException(404, "Service not found")
        try:
            booking = commerce.book_slot(conn, service_id, email, float(slot_ts), name)
        except (ValueError, TypeError):
            raise HTTPException(409, "Slot no longer available")
        contact = db.upsert_contact(conn, _default_audience(conn), email, first_name=name or None,
                                    signup_source=f"booking:{service['name']}")
        db.attach_tag(conn, contact["id"], "booked")
        dispatch_async("booking_confirmed", {"email": email, "service": service["name"],
                                             "slot": slot_ts})
    when = dt.datetime.utcfromtimestamp(booking["slot_ts"]).strftime('%a %d %b %Y, %H:%M UTC')
    return _page("Booking confirmed", f"<h1>You're booked!</h1>"
                 f"<p class='muted'>{_esc(service['name'])} · <b>{when}</b>. "
                 f"A calendar invite / confirmation would be sent to {_esc(email)}.</p>")


# ---------- referrals + creator network clicks ------------------------------------------------

@router.get("/r/{code}", response_class=HTMLResponse)
def referral_link(code: str):
    with db.get_db() as conn:
        referrer = conn.execute("SELECT id FROM contacts WHERE id = ?", (code,)).fetchone()
        if referrer:
            record_event(conn, None, "referral_link_click", value=code)
    return RedirectResponse(f"/blog?ref={_esc(code)}")


@router.get("/r/reco/{rec_id}", response_class=HTMLResponse)
def reco_click(rec_id: str):
    with db.get_db() as conn:
        rec = conn.execute("SELECT * FROM creator_recommendations WHERE id = ?", (rec_id,)).fetchone()
        if not rec:
            raise HTTPException(404, "Recommendation not found")
        conn.execute("UPDATE creator_recommendations SET clicks = clicks + 1 WHERE id = ?", (rec_id,))
    response = RedirectResponse(rec["partner_url"])
    return response

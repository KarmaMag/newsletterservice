"""Publishing router: website/blog posts, landing pages with variants, signup forms."""
import json
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import db

router = APIRouter(prefix="/api")


class PostIn(BaseModel):
    title: str
    subtitle: Optional[str] = None
    html_content: str = ""
    visibility: str = "public"


class PostPatch(BaseModel):
    title: Optional[str] = None
    subtitle: Optional[str] = None
    html_content: Optional[str] = None
    status: Optional[str] = None
    visibility: Optional[str] = None


class PageIn(BaseModel):
    title: str
    slug: Optional[str] = None
    html_content: str = ""
    page_type: str = "landing"
    audience_id: Optional[str] = None
    published: bool = True


class PagePatch(BaseModel):
    title: Optional[str] = None
    html_content: Optional[str] = None
    published: Optional[bool] = None


class VariantIn(BaseModel):
    label: str
    html_content: str
    weight: float = 1.0


class FormIn(BaseModel):
    name: str
    audience_id: str
    headline: str = "Subscribe to our newsletter"
    button_text: str = "Subscribe"
    thank_you_message: str = "Thanks! Check your inbox."
    tags: list[str] = []
    source: str = "form"


def _slug(text: str, table: str, conn) -> str:
    base = "".join(c if c.isalnum() else "-" for c in text.lower()).strip("-")[:60] or "item"
    slug, n = base, 1
    while conn.execute(f"SELECT 1 FROM {table} WHERE slug = ?", (slug,)).fetchone():
        n += 1
        slug = f"{base}-{n}"
    return slug


def _or_404(row, what: str) -> dict:
    if not row:
        raise HTTPException(404, f"{what} not found")
    return db.row_to_dict(row)


# ---------- posts (blog / newsletter archive) -------------------------------------

@router.get("/posts")
def list_posts(status: Optional[str] = None):
    with db.get_db() as conn:
        query = "SELECT id, title, subtitle, slug, status, visibility, published_at, created_at FROM posts"
        params = []
        if status:
            query += " WHERE status = ?"
            params.append(status)
        query += " ORDER BY created_at DESC"
        return [db.row_to_dict(r) for r in conn.execute(query, params)]


@router.post("/posts", status_code=201)
def create_post(payload: PostIn):
    with db.get_db() as conn:
        post_id = db.new_id()
        slug = _slug(payload.title, "posts", conn)
        conn.execute(
            """INSERT INTO posts (id, slug, title, subtitle, html_content, status, visibility, created_at)
               VALUES (?, ?, ?, ?, ?, 'draft', ?, ?)""",
            (post_id, slug, payload.title, payload.subtitle, payload.html_content,
             payload.visibility, db.now()),
        )
        return _or_404(conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone(), "Post")


@router.get("/posts/{post_id}")
def get_post(post_id: str):
    with db.get_db() as conn:
        return _or_404(conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone(), "Post")


@router.patch("/posts/{post_id}")
def update_post(post_id: str, payload: PostPatch):
    with db.get_db() as conn:
        _or_404(conn.execute("SELECT id FROM posts WHERE id = ?", (post_id,)).fetchone(), "Post")
        for key in ("title", "subtitle", "html_content", "status", "visibility"):
            value = getattr(payload, key)
            if value is not None:
                if key == "status" and value == "published":
                    conn.execute(
                        "UPDATE posts SET status = 'published', published_at = ? WHERE id = ?",
                        (db.now(), post_id),
                    )
                else:
                    conn.execute(f"UPDATE posts SET {key} = ? WHERE id = ?", (value, post_id))
        return _or_404(conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone(), "Post")


@router.delete("/posts/{post_id}", status_code=204)
def delete_post(post_id: str):
    with db.get_db() as conn:
        if conn.execute("DELETE FROM posts WHERE id = ?", (post_id,)).rowcount == 0:
            raise HTTPException(404, "Post not found")


# ---------- landing pages / website pages -----------------------------------------

@router.get("/pages")
def list_pages():
    with db.get_db() as conn:
        rows = conn.execute("SELECT * FROM pages ORDER BY created_at DESC").fetchall()
        pages = []
        for r in rows:
            page = db.row_to_dict(r)
            page["views"] = conn.execute(
                "SELECT COUNT(*) c FROM subscriber_events WHERE type = 'page_view' AND source = ?",
                (f"page:{r['id']}",),
            ).fetchone()["c"]
            page["signups"] = conn.execute(
                "SELECT COUNT(*) c FROM subscriber_events WHERE type = 'form_signup' AND source = ?",
                (f"page:{r['id']}",),
            ).fetchone()["c"]
            pages.append(page)
        return pages


@router.post("/pages", status_code=201)
def create_page(payload: PageIn):
    if payload.page_type not in ("landing", "page"):
        raise HTTPException(400, "page_type must be 'landing' or 'page'")
    with db.get_db() as conn:
        page_id = db.new_id()
        slug = payload.slug or _slug(payload.title, "pages", conn)
        row = conn.execute("SELECT 1 FROM pages WHERE slug = ?", (slug,)).fetchone()
        if row:
            raise HTTPException(400, f"Slug '{slug}' already exists")
        conn.execute(
            """INSERT INTO pages (id, slug, title, html_content, page_type, audience_id, published, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (page_id, slug, payload.title, payload.html_content, payload.page_type,
             payload.audience_id, int(payload.published), db.now()),
        )
        return _or_404(conn.execute("SELECT * FROM pages WHERE id = ?", (page_id,)).fetchone(), "Page")


@router.patch("/pages/{page_id}")
def update_page(page_id: str, payload: PagePatch):
    with db.get_db() as conn:
        _or_404(conn.execute("SELECT id FROM pages WHERE id = ?", (page_id,)).fetchone(), "Page")
        for key in ("title", "html_content", "published"):
            value = getattr(payload, key)
            if value is not None:
                conn.execute(f"UPDATE pages SET {key} = ? WHERE id = ?", (value, page_id))
        return _or_404(conn.execute("SELECT * FROM pages WHERE id = ?", (page_id,)).fetchone(), "Page")


@router.delete("/pages/{page_id}", status_code=204)
def delete_page(page_id: str):
    with db.get_db() as conn:
        conn.execute("DELETE FROM page_variants WHERE page_id = ?", (page_id,))
        if conn.execute("DELETE FROM pages WHERE id = ?", (page_id,)).rowcount == 0:
            raise HTTPException(404, "Page not found")


@router.post("/pages/{page_id}/variants", status_code=201)
def add_page_variant(page_id: str, payload: VariantIn):
    with db.get_db() as conn:
        _or_404(conn.execute("SELECT id FROM pages WHERE id = ?", (page_id,)).fetchone(), "Page")
        variant_id = db.new_id()
        conn.execute(
            """INSERT INTO page_variants (id, page_id, label, html_content, weight)
               VALUES (?, ?, ?, ?, ?)""",
            (variant_id, page_id, payload.label, payload.html_content, payload.weight),
        )
        return _or_404(conn.execute("SELECT * FROM page_variants WHERE id = ?", (variant_id,)).fetchone(), "Variant")


@router.post("/pages/{page_id}/ab-report")
def page_ab_report(page_id: str):
    with db.get_db() as conn:
        page = _or_404(conn.execute("SELECT * FROM pages WHERE id = ?", (page_id,)).fetchone(), "Page")
        variants = conn.execute("SELECT label FROM page_variants WHERE page_id = ?", (page_id,)).fetchall()
        if variants:
            labels = [v["label"] for v in variants]
            source_of = lambda label: f"page:{page_id}:{label}"  # noqa: E731
        else:
            labels = ["A"]
            source_of = lambda label: f"page:{page_id}"  # noqa: E731
        report = []
        conversion_type = "page_conversion" if variants else "form_signup"
        for label in labels:
            views = conn.execute(
                "SELECT COUNT(*) c FROM subscriber_events WHERE type = 'page_view' AND source = ?",
                (source_of(label),),
            ).fetchone()["c"]
            conversions = conn.execute(
                "SELECT COUNT(*) c FROM subscriber_events WHERE type = ? AND source = ?",
                (conversion_type, source_of(label)),
            ).fetchone()["c"]
            report.append({
                "label": label,
                "views": views,
                "conversions": conversions,
                "conversion_rate": round(conversions / views, 4) if views else 0.0,
            })
        best = max(report, key=lambda r: r["conversion_rate"]) if report else None
        return {"page": page["slug"], "report": report, "winner": best["label"] if best else None}


# ---------- signup forms -----------------------------------------------------------

@router.get("/forms")
def list_forms():
    with db.get_db() as conn:
        rows = conn.execute("SELECT * FROM forms ORDER BY created_at DESC").fetchall()
        return [db.row_to_dict(r) for r in rows]


@router.post("/forms", status_code=201)
def create_form(payload: FormIn):
    with db.get_db() as conn:
        form_id = db.new_id()
        conn.execute(
            """INSERT INTO forms (id, name, audience_id, headline, button_text,
                                  thank_you_message, tags, source, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (form_id, payload.name, payload.audience_id, payload.headline,
             payload.button_text, payload.thank_you_message,
             json.dumps(payload.tags), payload.source, db.now()),
        )
        return _or_404(conn.execute("SELECT * FROM forms WHERE id = ?", (form_id,)).fetchone(), "Form")


@router.get("/forms/{form_id}")
def get_form(form_id: str):
    with db.get_db() as conn:
        form = _or_404(conn.execute("SELECT * FROM forms WHERE id = ?", (form_id,)).fetchone(), "Form")
        form["tags"] = json.loads(form["tags"])
        form["submissions"] = conn.execute(
            "SELECT COUNT(*) c FROM subscriber_events WHERE type = 'form_signup' AND source = ?",
            (f"form:{form_id}",),
        ).fetchone()["c"]
        return form


@router.delete("/forms/{form_id}", status_code=204)
def delete_form(form_id: str):
    with db.get_db() as conn:
        if conn.execute("DELETE FROM forms WHERE id = ?", (form_id,)).rowcount == 0:
            raise HTTPException(404, "Form not found")

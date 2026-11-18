"""Commerce router: digital products, orders, paid subscriptions, services/bookings."""
import datetime as dt
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, EmailStr

from .. import commerce, db

router = APIRouter(prefix="/api")


class ProductIn(BaseModel):
    name: str
    price_cents: int = 0
    product_type: str = "download"
    description: str = ""
    file_url: Optional[str] = None
    published: bool = False


class OrderIn(BaseModel):
    product_id: str
    email: EmailStr
    status: str = "paid"


class PlanIn(BaseModel):
    name: str
    price_cents: int
    interval: str = "monthly"
    description: str = ""
    published: bool = False


class SubscribeIn(BaseModel):
    plan_id: str
    email: EmailStr


class ServiceIn(BaseModel):
    name: str
    description: str = ""
    duration_min: int = 30
    price_cents: int = 0
    availability: str = ""
    published: bool = False


class BookingIn(BaseModel):
    email: EmailStr
    slot_ts: float
    name: Optional[str] = None


def _or_404(row, what: str) -> dict:
    if not row:
        raise HTTPException(404, f"{what} not found")
    return db.row_to_dict(row)


# ---------- products / orders ------------------------------------------------------

@router.get("/products")
def list_products(published: Optional[bool] = None):
    with db.get_db() as conn:
        query = "SELECT * FROM products"
        params = []
        if published is not None:
            query += " WHERE published = ?"
            params.append(int(published))
        query += " ORDER BY created_at DESC"
        return [db.row_to_dict(r) for r in conn.execute(query, params)]


@router.post("/products", status_code=201)
def create_product(payload: ProductIn):
    if payload.product_type not in ("download", "course", "coaching", "appointment"):
        raise HTTPException(400, "product_type must be download, course, coaching or appointment")
    with db.get_db() as conn:
        return commerce.create_product(
            conn, payload.name, payload.price_cents, payload.product_type,
            payload.description, payload.file_url, payload.published,
        )


@router.patch("/products/{product_id}")
def update_product(product_id: str, body: dict):
    with db.get_db() as conn:
        _or_404(conn.execute("SELECT id FROM products WHERE id = ?", (product_id,)).fetchone(), "Product")
        for key in ("name", "description", "price_cents", "file_url", "product_type", "published"):
            if key in body:
                conn.execute(f"UPDATE products SET {key} = ? WHERE id = ?",
                             (body[key], product_id))
        return _or_404(conn.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone(), "Product")


@router.delete("/products/{product_id}", status_code=204)
def delete_product(product_id: str):
    with db.get_db() as conn:
        conn.execute("DELETE FROM orders WHERE product_id = ?", (product_id,))
        if conn.execute("DELETE FROM products WHERE id = ?", (product_id,)).rowcount == 0:
            raise HTTPException(404, "Product not found")


@router.post("/orders", status_code=201)
def create_order(payload: OrderIn):
    with db.get_db() as conn:
        _or_404(conn.execute("SELECT id FROM products WHERE id = ?", (payload.product_id,)).fetchone(), "Product")
        return commerce.create_order(conn, payload.product_id, payload.email, payload.status)


@router.get("/orders")
def list_orders(limit: int = Query(100, le=500)):
    with db.get_db() as conn:
        rows = conn.execute(
            """SELECT o.*, p.name AS product_name FROM orders o
               JOIN products p ON p.id = o.product_id
               ORDER BY o.created_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [db.row_to_dict(r) for r in rows]


# ---------- paid subscriptions -------------------------------------------------------

@router.get("/subscription-plans")
def list_plans():
    with db.get_db() as conn:
        rows = conn.execute("SELECT * FROM subscription_plans ORDER BY created_at DESC").fetchall()
        return [db.row_to_dict(r) for r in rows]


@router.post("/subscription-plans", status_code=201)
def create_plan(payload: PlanIn):
    if payload.interval not in ("monthly", "yearly"):
        raise HTTPException(400, "interval must be monthly or yearly")
    with db.get_db() as conn:
        return commerce.create_plan(conn, payload.name, payload.price_cents,
                                    payload.interval, payload.description, payload.published)


@router.post("/subscriptions", status_code=201)
def subscribe(payload: SubscribeIn):
    with db.get_db() as conn:
        try:
            return commerce.create_subscription(conn, payload.plan_id, payload.email)
        except ValueError as exc:
            raise HTTPException(404, str(exc))


@router.get("/subscriptions")
def list_subscriptions(status: Optional[str] = None):
    with db.get_db() as conn:
        query = """SELECT s.*, p.name AS plan_name, p.price_cents, p.interval
                   FROM subscriptions s JOIN subscription_plans p ON p.id = s.plan_id"""
        params = []
        if status:
            query += " WHERE s.status = ?"
            params.append(status)
        query += " ORDER BY s.started_at DESC"
        return [db.row_to_dict(r) for r in conn.execute(query, params)]


@router.post("/subscriptions/{subscription_id}/cancel")
def cancel_subscription(subscription_id: str):
    with db.get_db() as conn:
        _or_404(conn.execute("SELECT id FROM subscriptions WHERE id = ?", (subscription_id,)).fetchone(), "Subscription")
        commerce.cancel_subscription(conn, subscription_id)
        return {"status": "canceled"}


# ---------- bookings -----------------------------------------------------------------

@router.get("/services")
def list_services():
    with db.get_db() as conn:
        rows = conn.execute("SELECT * FROM services ORDER BY created_at DESC").fetchall()
        return [db.row_to_dict(r) for r in rows]


@router.post("/services", status_code=201)
def create_service(payload: ServiceIn):
    if payload.availability:
        try:
            parsed = commerce.parse_availability(payload.availability)
        except Exception:
            parsed = None
        if payload.availability and not parsed:
            raise HTTPException(400, 'availability must be JSON like {"Mon": ["09:00-17:00"]}')
    with db.get_db() as conn:
        service_id = db.new_id()
        conn.execute(
            """INSERT INTO services (id, name, description, duration_min, price_cents,
                                     availability, published, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (service_id, payload.name, payload.description, payload.duration_min,
             payload.price_cents, payload.availability or "", int(payload.published), db.now()),
        )
        row = conn.execute("SELECT * FROM services WHERE id = ?", (service_id,)).fetchone()
        service = db.row_to_dict(row)
        service["availability_json"] = commerce.parse_availability(service["availability"])
        return service


@router.get("/services/{service_id}/slots")
def list_slots(service_id: str, days: int = Query(14, ge=1, le=60)):
    with db.get_db() as conn:
        row = conn.execute("SELECT id, name FROM services WHERE id = ?", (service_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Service not found")
        slots = commerce.available_slots(conn, service_id, days)
        return {
            "service": db.row_to_dict(row),
            "slots": [
                {"ts": s, "iso": dt.datetime.utcfromtimestamp(s).isoformat() + "Z"}
                for s in slots
            ],
        }


@router.post("/services/{service_id}/book", status_code=201)
def book(service_id: str, payload: BookingIn):
    with db.get_db() as conn:
        try:
            return commerce.book_slot(conn, service_id, payload.email, payload.slot_ts, payload.name)
        except ValueError as exc:
            raise HTTPException(409, str(exc))


@router.get("/bookings")
def list_bookings():
    with db.get_db() as conn:
        rows = conn.execute(
            """SELECT b.*, s.name AS service_name FROM bookings b
               JOIN services s ON s.id = b.service_id
               ORDER BY b.slot_ts"""
        ).fetchall()
        return [db.row_to_dict(r) for r in rows]

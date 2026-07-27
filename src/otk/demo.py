"""Sample data, so the TUI can be exercised without a live Odoo instance."""

from __future__ import annotations

import io
from typing import Any

from .service import IncomingFile, Principal, Store


def _fake_screenshot(caption: str, colour: tuple[int, int, int]) -> bytes:
    """Render a placeholder PNG that stands in for a real Odoo screenshot."""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (1280, 720), (245, 245, 247))
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, 1280, 46], fill=(113, 75, 103))  # Odoo's navbar purple
    draw.rectangle([40, 90, 1240, 300], outline=colour, width=3)
    draw.text((60, 120), caption, fill=(30, 30, 30))
    draw.text((60, 160), "This is placeholder demo data, not a real screenshot.", fill=(90, 90, 90))
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    return buffer.getvalue()


DEMO_TICKETS: list[dict[str, Any]] = [
    {
        "title": "Cannot confirm sale order — 'Insufficient stock' on a stocked product",
        "description": (
            "Confirming SO12043 raises a validation error even though the product shows "
            "42 units on hand in WH/Stock. It started this morning; the same order "
            "confirmed fine on Friday."
        ),
        "reporter": {"name": "Marta Silva", "email": "marta@acme.example", "odoo_uid": 7},
        "priority": "high",
        "category": "bug",
        "colour": (200, 60, 60),
        "context": {
            "url": "https://acme.odoo.example/odoo/sales/12043",
            "page_title": "SO12043 - Sales Order",
            "odoo_version": "17.0",
            "database": "acme-prod",
            "model": "sale.order",
            "res_id": 12043,
            "view_type": "form",
            "company_name": "Acme Industries SA",
            "user_lang": "pt_PT",
            "user_tz": "Europe/Lisbon",
            "error": {
                "name": "odoo.exceptions.UserError",
                "message": "Insufficient stock for product [FURN-0042] Office Chair",
                "rpc_route": "/web/dataset/call_button",
                "rpc_model": "sale.order",
                "rpc_method": "action_confirm",
                "http_status": 200,
            },
            "browser": {
                "user_agent": "Mozilla/5.0 (X11; Linux x86_64) Chrome/126.0",
                "viewport_width": 1512,
                "viewport_height": 780,
            },
        },
    },
    {
        "title": "Invoice PDF shows the wrong company logo",
        "description": (
            "Customer invoices print with the old logo. We uploaded the new one in "
            "Settings > Companies two weeks ago."
        ),
        "reporter": {"name": "João Pereira", "email": "joao@acme.example", "odoo_uid": 3},
        "priority": "normal",
        "category": "bug",
        "colour": (200, 150, 40),
        "context": {
            "url": "https://acme.odoo.example/odoo/accounting/45021",
            "page_title": "INV/2026/00512",
            "odoo_version": "17.0",
            "database": "acme-prod",
            "model": "account.move",
            "res_id": 45021,
            "view_type": "form",
            "user_lang": "pt_PT",
        },
    },
    {
        "title": "How do I bulk-update customer payment terms?",
        "description": "We need to move about 300 customers from 30 days to 60 days.",
        "reporter": {"name": "Ana Costa", "email": "ana@acme.example", "odoo_uid": 12},
        "priority": "low",
        "category": "how_to",
        "colour": (60, 130, 200),
        "context": {
            "url": "https://acme.odoo.example/odoo/contacts",
            "page_title": "Contacts",
            "odoo_version": "17.0",
            "database": "acme-prod",
            "model": "res.partner",
            "view_type": "list",
            "user_lang": "pt_PT",
        },
    },
    {
        "title": "Inventory report times out after the last update",
        "description": (
            "The stock valuation report spins for ~60s and then returns a gateway error. "
            "Reproducible for any date range wider than one month."
        ),
        "reporter": {"name": "Marta Silva", "email": "marta@acme.example", "odoo_uid": 7},
        "priority": "urgent",
        "category": "performance",
        "colour": (200, 60, 60),
        "context": {
            "url": "https://acme.odoo.example/odoo/inventory/reporting/valuation",
            "page_title": "Stock Valuation",
            "odoo_version": "17.0",
            "database": "acme-prod",
            "model": "stock.valuation.layer",
            "view_type": "list",
            "error": {
                "name": "GatewayTimeout",
                "message": "504 Gateway Time-out",
                "rpc_route": "/web/dataset/call_kw",
                "http_status": 504,
            },
        },
    },
]


def seed_demo_data(store: Store) -> None:
    try:
        client = store.get_client_by_slug("acme")
    except Exception:
        client = store.create_client(
            name="Acme Industries",
            slug="acme",
            contact_email="it@acme.example",
            odoo_url="https://acme.odoo.example",
        )
        store.issue_api_key(client.id, name="demo")

    principal = Principal(
        auth_type="api_key",
        client_id=client.id,
        client_slug=client.slug,
        client_name=client.name,
        api_key_id="demo",
        api_key_name="demo",
    )

    for spec in DEMO_TICKETS:
        screenshot = _fake_screenshot(spec["title"][:60], spec["colour"])
        ticket = store.create_ticket(
            principal,
            title=spec["title"],
            description=spec["description"],
            reporter=spec["reporter"],
            priority=spec["priority"],
            category=spec["category"],
            context=spec["context"],
            files=[
                IncomingFile(
                    data=screenshot,
                    filename="screenshot.png",
                    content_type="image/png",
                    role="screenshot",
                )
            ],
        )
        if spec["priority"] == "urgent":
            store.add_comment(
                ticket.id,
                body="Looking into this now — can you confirm the exact date range used?",
                author_type="agent",
                author_name="José",
            )
            store.add_comment(
                ticket.id,
                body="01/01/2026 to 31/03/2026. Anything under a month works fine.",
                author_type="client",
                author_name=spec["reporter"]["name"],
            )

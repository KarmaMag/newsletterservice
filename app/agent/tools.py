"""Agent tool registry: every capability exposed to the LLM agent, mapped to
service actions. Used by both the Mistral-powered CLI agent and the MCP server."""
import json
from typing import Any, Callable, Optional

from .. import mailer


def _api(method: str, path: str, json_body: Optional[dict] = None) -> Any:
    import httpx

    base = "http://localhost:8000"
    with httpx.Client(timeout=60) as client:
        resp = client.request(method, f"{base}{path}", json=json_body)
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("detail", resp.text)
        except ValueError:
            detail = resp.text
        raise RuntimeError(f"API {method} {path} -> {resp.status_code}: {detail}")
    return resp.json() if resp.content else None


# ---------- tool implementations ---------------------------------------------

def tool_stats_(_: dict) -> dict:
    return _api("GET", "/api/stats")


def tool_list_audiences(_: dict) -> list:
    return _api("GET", "/api/audiences")


def tool_create_audience(args: dict) -> dict:
    return _api("POST", "/api/audiences", {"name": args["name"]})


def tool_list_contacts(args: dict) -> dict:
    params = []
    if args.get("audience_id"):
        params.append(f"audience_id={args['audience_id']}")
    if args.get("search"):
        params.append(f"search={args['search']}")
    if args.get("status"):
        params.append(f"status={args['status']}")
    if args.get("tag"):
        params.append(f"tag={args['tag']}")
    params.append(f"limit={min(int(args.get('limit', 20)), 500)}")
    query = "&" + "&".join(params) if params else ""
    return _api("GET", f"/api/contacts{query}")


def tool_add_contact(args: dict) -> dict:
    return _api("POST", "/api/contacts", {
        "email": args["email"],
        "audience_id": args.get("audience_id") or _first_audience_id(),
        "first_name": args.get("first_name"),
        "last_name": args.get("last_name"),
        "tags": args.get("tags", []),
        "status": args.get("status", "subscribed"),
    })


def tool_remove_contact(args: dict) -> None:
    _api("DELETE", f"/api/contacts/{args['contact_id']}")


def tool_tag_contacts(args: dict) -> dict:
    return _api("PATCH", f"/api/contacts/{args['contact_id']}", {
        "add_tags": args["tags"],
    })


def tool_unsubscribe_contact(args: dict) -> dict:
    return _api("POST", "/api/unsubscribe", {"email": args["email"]})


def tool_list_campaigns(_: dict) -> list:
    return _api("GET", "/api/campaigns")


def tool_get_campaign(args: dict) -> dict:
    return _api("GET", f"/api/campaigns/{args['campaign_id']}")


def tool_create_campaign(args: dict) -> dict:
    return _api("POST", "/api/campaigns", {
        "audience_id": args.get("audience_id") or _first_audience_id(),
        "name": args["name"],
        "subject": args["subject"],
        "preheader": args.get("preheader"),
        "from_name": args.get("from_name"),
        "from_email": args.get("from_email"),
        "html_content": args["html_content"],
    })


def tool_update_campaign(args: dict) -> dict:
    body = {k: args[k] for k in ("name", "subject", "preheader", "html_content", "status")
            if k in args}
    return _api("PATCH", f"/api/campaigns/{args['campaign_id']}", body)


def tool_send_campaign(args: dict) -> dict:
    query = "&dry_run=true" if args.get("dry_run") else ""
    return _api("POST", f"/api/campaigns/{args['campaign_id']}/send{query}")


def tool_preview_campaign(args: dict) -> dict:
    return _api("POST", f"/api/campaigns/{args['campaign_id']}/preview",
                {"email": args["email"]})


def tool_campaign_stats(args: dict) -> dict:
    return _api("GET", f"/api/campaigns/{args['campaign_id']}/stats")


def tool_send_test_email(args: dict) -> dict:
    if not mailer.is_configured():
        raise RuntimeError("SMTP is not configured on the server.")
    mailer.send_email(args["to"], args["subject"], args["html"],
                      unsubscribe_url=None)
    return {"status": "sent", "to": args["to"]}


def tool_import_mailchimp(args: dict) -> dict:
    body: dict = {"api_key": args["api_key"], "with_activity": bool(args.get("with_activity", False))}
    if args.get("base_url"):
        body["base_url"] = args["base_url"]
    return _api("POST", "/api/import/mailchimp", body)


def tool_list_tags(_: dict) -> list:
    return _api("GET", "/api/tags")


def tool_create_template(args: dict) -> dict:
    return _api("POST", "/api/templates", {
        "name": args["name"], "subject": args.get("subject", ""),
        "html_content": args["html_content"],
    })


def tool_create_sequence(args: dict) -> dict:
    return _api("POST", "/api/automations/sequences", {
        "name": args["name"],
        "audience_id": args.get("audience_id"),
        "trigger_type": args.get("trigger_type", "manual"),
        "trigger_value": args.get("trigger_value"),
    })


def tool_add_sequence_step(args: dict) -> dict:
    return _api("POST", f"/api/automations/sequences/{args['sequence_id']}/steps", {
        "subject": args["subject"], "html_content": args["html_content"],
        "position": args.get("position", 0), "delay_hours": args.get("delay_hours", 0),
    })


def tool_activate_sequence(args: dict) -> dict:
    return _api("PATCH", f"/api/automations/sequences/{args['sequence_id']}",
                {"status": "active"})


def tool_enroll_sequence(args: dict) -> dict:
    body = {k: args[k] for k in ("contact_id", "segment_id", "tag") if args.get(k)}
    return _api("POST", f"/api/automations/sequences/{args['sequence_id']}/enroll", body)


def tool_automation_tick(_: dict) -> dict:
    return _api("POST", "/api/automations/tick")


def tool_create_segment(args: dict) -> dict:
    return _api("POST", "/api/segments",
                {"name": args["name"], "rules": args.get("rules", [])})


def tool_preview_segment(args: dict) -> dict:
    return _api("POST", f"/api/segments/{args['segment_id']}/preview",
                {} if not args.get("limit") else {"limit": args["limit"]})


def tool_publish_post(args: dict) -> dict:
    post = _api("POST", "/api/posts", {
        "title": args["title"], "subtitle": args.get("subtitle"),
        "html_content": args["html_content"],
        "visibility": args.get("visibility", "public"),
    })
    return _api("PATCH", f"/api/posts/{post['id']}", {"status": "published"})


def tool_create_landing_page(args: dict) -> dict:
    body = {
        "title": args["title"], "html_content": args.get("html_content", ""),
        "audience_id": args.get("audience_id"), "published": True,
    }
    if args.get("slug"):
        body["slug"] = args["slug"]
    return _api("POST", "/api/pages", body)


def tool_create_ab_test(args: dict) -> dict:
    return _api("POST", f"/api/campaigns/{args['campaign_id']}/ab-tests",
                {"name": args["name"], "variants": args["variants"]})


def tool_send_ab_test(args: dict) -> dict:
    return _api("POST", f"/api/ab-tests/{args['test_id']}/send",
                {} if not args.get("dry_run") else {})


def tool_ab_winner(args: dict) -> dict:
    return _api("POST", f"/api/ab-tests/{args['test_id']}/winner")


def tool_create_product(args: dict) -> dict:
    return _api("POST", "/api/products", {
        "name": args["name"], "price_cents": args["price_cents"],
        "product_type": args.get("product_type", "download"),
        "description": args.get("description", ""),
        "file_url": args.get("file_url"), "published": args.get("published", True),
    })


def tool_list_orders(_: dict) -> list:
    return _api("GET", "/api/orders")


def tool_create_subscription_plan(args: dict) -> dict:
    return _api("POST", "/api/subscription-plans", {
        "name": args["name"], "price_cents": args["price_cents"],
        "interval": args.get("interval", "monthly"),
        "description": args.get("description", ""), "published": True,
    })


def tool_create_service(args: dict) -> dict:
    return _api("POST", "/api/services", {
        "name": args["name"], "description": args.get("description", ""),
        "duration_min": args.get("duration_min", 30),
        "price_cents": args.get("price_cents", 0),
        "availability": args.get("availability", ""), "published": True,
    })


def tool_list_bookings(_: dict) -> list:
    return _api("GET", "/api/bookings")


def tool_analytics_overview(_: dict) -> dict:
    return _api("GET", "/api/analytics/overview")


def tool_creator_analytics(_: dict) -> dict:
    return _api("GET", "/api/analytics/creator")


def tool_referral_stats(args: dict) -> dict:
    return _api("GET", f"/api/referrals/{args['contact_id']}")


def tool_creator_network_embed(_: dict) -> dict:
    return _api("POST", "/api/recommendations/embed")


def tool_create_sponsorship(args: dict) -> dict:
    return _api("POST", "/api/sponsorships", {
        "sponsor_name": args["sponsor_name"], "sponsor_url": args.get("sponsor_url"),
        "ad_copy": args.get("ad_copy", ""), "price_cents": args.get("price_cents", 0),
    })


def _first_audience_id() -> str:
    audiences = _api("GET", "/api/audiences")
    if not audiences:
        raise RuntimeError("No audiences exist yet; create one first.")
    return audiences[0]["id"]


# ---------- tool schemas -------------------------------------------------------

ToolFn = Callable[[dict], Any]

TOOLS: dict[str, ToolFn] = {
    "get_service_stats": tool_stats_,
    "list_audiences": tool_list_audiences,
    "create_audience": tool_create_audience,
    "list_contacts": tool_list_contacts,
    "add_contact": tool_add_contact,
    "remove_contact": tool_remove_contact,
    "tag_contact": tool_tag_contacts,
    "unsubscribe_contact": tool_unsubscribe_contact,
    "list_campaigns": tool_list_campaigns,
    "get_campaign": tool_get_campaign,
    "create_campaign": tool_create_campaign,
    "update_campaign": tool_update_campaign,
    "send_campaign": tool_send_campaign,
    "send_campaign_preview": tool_preview_campaign,
    "get_campaign_stats": tool_campaign_stats,
    "send_test_email": tool_send_test_email,
    "import_mailchimp_data": tool_import_mailchimp,
    "list_tags": tool_list_tags,
    "create_template": tool_create_template,
    "create_sequence": tool_create_sequence,
    "add_sequence_step": tool_add_sequence_step,
    "activate_sequence": tool_activate_sequence,
    "enroll_sequence": tool_enroll_sequence,
    "automation_tick": tool_automation_tick,
    "create_segment": tool_create_segment,
    "preview_segment": tool_preview_segment,
    "publish_post": tool_publish_post,
    "create_landing_page": tool_create_landing_page,
    "create_ab_test": tool_create_ab_test,
    "send_ab_test": tool_send_ab_test,
    "ab_winner": tool_ab_winner,
    "create_product": tool_create_product,
    "list_orders": tool_list_orders,
    "create_subscription_plan": tool_create_subscription_plan,
    "create_service": tool_create_service,
    "list_bookings": tool_list_bookings,
    "analytics_overview": tool_analytics_overview,
    "creator_analytics": tool_creator_analytics,
    "referral_stats": tool_referral_stats,
    "creator_network_embed": tool_creator_network_embed,
    "create_sponsorship": tool_create_sponsorship,
}

PARAMETERS = {
    "get_service_stats": {"type": "object", "properties": {}, "required": []},
    "list_audiences": {"type": "object", "properties": {}, "required": []},
    "create_audience": {
        "type": "object",
        "properties": {"name": {"type": "string"}},
        "required": ["name"],
    },
    "list_contacts": {
        "type": "object",
        "properties": {
            "audience_id": {"type": "string"},
            "search": {"type": "string"},
            "status": {"type": "string",
                       "enum": ["subscribed", "unsubscribed", "pending", "cleaned",
                                "transactional"]},
            "tag": {"type": "string"},
            "limit": {"type": "integer"},
        },
        "required": [],
    },
    "add_contact": {
        "type": "object",
        "properties": {
            "email": {"type": "string"},
            "audience_id": {"type": "string"},
            "first_name": {"type": "string"},
            "last_name": {"type": "string"},
            "tags": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["email"],
    },
    "remove_contact": {
        "type": "object",
        "properties": {"contact_id": {"type": "string"}},
        "required": ["contact_id"],
    },
    "tag_contact": {
        "type": "object",
        "properties": {
            "contact_id": {"type": "string"},
            "tags": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["contact_id", "tags"],
    },
    "unsubscribe_contact": {
        "type": "object",
        "properties": {"email": {"type": "string"}},
        "required": ["email"],
    },
    "list_campaigns": {"type": "object", "properties": {}, "required": []},
    "get_campaign": {
        "type": "object",
        "properties": {"campaign_id": {"type": "string"}},
        "required": ["campaign_id"],
    },
    "create_campaign": {
        "type": "object",
        "properties": {
            "audience_id": {"type": "string"},
            "name": {"type": "string"},
            "subject": {"type": "string"},
            "preheader": {"type": "string"},
            "from_name": {"type": "string"},
            "from_email": {"type": "string"},
            "html_content": {"type": "string",
                             "description": "Full HTML body of the newsletter"},
        },
        "required": ["name", "subject", "html_content"],
    },
    "update_campaign": {
        "type": "object",
        "properties": {
            "campaign_id": {"type": "string"},
            "name": {"type": "string"},
            "subject": {"type": "string"},
            "preheader": {"type": "string"},
            "html_content": {"type": "string"},
            "status": {"type": "string", "enum": ["draft", "archived"]},
        },
        "required": ["campaign_id"],
    },
    "send_campaign": {
        "type": "object",
        "properties": {
            "campaign_id": {"type": "string"},
            "dry_run": {"type": "boolean",
                        "description": "If true, only count recipients without sending"},
        },
        "required": ["campaign_id"],
    },
    "send_campaign_preview": {
        "type": "object",
        "properties": {
            "campaign_id": {"type": "string"},
            "email": {"type": "string"},
        },
        "required": ["campaign_id", "email"],
    },
    "get_campaign_stats": {
        "type": "object",
        "properties": {"campaign_id": {"type": "string"}},
        "required": ["campaign_id"],
    },
    "send_test_email": {
        "type": "object",
        "properties": {
            "to": {"type": "string"},
            "subject": {"type": "string"},
            "html": {"type": "string"},
        },
        "required": ["to", "subject", "html"],
    },
    "import_mailchimp_data": {
        "type": "object",
        "properties": {
            "api_key": {"type": "string",
                        "description": "Mailchimp API key with datacenter suffix, e.g. xxx-us21"},
            "with_activity": {"type": "boolean",
                              "description": "Also import open/click history (slower)"},
            "base_url": {"type": "string",
                         "description": "Override API base URL (testing only)"},
        },
        "required": ["api_key"],
    },
    "list_tags": {"type": "object", "properties": {}, "required": []},
    "create_template": {
        "type": "object",
        "properties": {"name": {"type": "string"}, "subject": {"type": "string"},
                       "html_content": {"type": "string"}},
        "required": ["name", "html_content"],
    },
    "create_sequence": {
        "type": "object",
        "properties": {
            "name": {"type": "string"}, "audience_id": {"type": "string"},
            "trigger_type": {"type": "string", "enum": ["manual", "signup", "tag_added", "purchased"]},
            "trigger_value": {"type": "string"},
        },
        "required": ["name"],
    },
    "add_sequence_step": {
        "type": "object",
        "properties": {
            "sequence_id": {"type": "string"}, "position": {"type": "integer"},
            "subject": {"type": "string"}, "html_content": {"type": "string"},
            "delay_hours": {"type": "number"},
        },
        "required": ["sequence_id", "subject", "html_content"],
    },
    "activate_sequence": {
        "type": "object",
        "properties": {"sequence_id": {"type": "string"}},
        "required": ["sequence_id"],
    },
    "enroll_sequence": {
        "type": "object",
        "properties": {
            "sequence_id": {"type": "string"}, "contact_id": {"type": "string"},
            "segment_id": {"type": "string"}, "tag": {"type": "string"},
        },
        "required": ["sequence_id"],
    },
    "automation_tick": {"type": "object", "properties": {}, "required": []},
    "create_segment": {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "rules": {"type": "array", "items": {"type": "object"}},
        },
        "required": ["name"],
    },
    "preview_segment": {
        "type": "object",
        "properties": {"segment_id": {"type": "string"}},
        "required": ["segment_id"],
    },
    "publish_post": {
        "type": "object",
        "properties": {
            "title": {"type": "string"}, "subtitle": {"type": "string"},
            "html_content": {"type": "string"},
            "visibility": {"type": "string", "enum": ["public", "paid"]},
        },
        "required": ["title", "html_content"],
    },
    "create_landing_page": {
        "type": "object",
        "properties": {
            "title": {"type": "string"}, "slug": {"type": "string"},
            "html_content": {"type": "string"}, "audience_id": {"type": "string"},
        },
        "required": ["title"],
    },
    "create_ab_test": {
        "type": "object",
        "properties": {
            "campaign_id": {"type": "string"}, "name": {"type": "string"},
            "variants": {"type": "array", "items": {"type": "object"}},
        },
        "required": ["campaign_id", "name", "variants"],
    },
    "send_ab_test": {
        "type": "object",
        "properties": {"test_id": {"type": "string"}},
        "required": ["test_id"],
    },
    "ab_winner": {
        "type": "object",
        "properties": {"test_id": {"type": "string"}},
        "required": ["test_id"],
    },
    "create_product": {
        "type": "object",
        "properties": {
            "name": {"type": "string"}, "price_cents": {"type": "integer"},
            "product_type": {"type": "string", "enum": ["download", "course", "coaching"]},
            "description": {"type": "string"}, "file_url": {"type": "string"},
        },
        "required": ["name", "price_cents"],
    },
    "list_orders": {"type": "object", "properties": {}, "required": []},
    "create_subscription_plan": {
        "type": "object",
        "properties": {
            "name": {"type": "string"}, "price_cents": {"type": "integer"},
            "interval": {"type": "string", "enum": ["monthly", "yearly"]},
            "description": {"type": "string"},
        },
        "required": ["name", "price_cents"],
    },
    "create_service": {
        "type": "object",
        "properties": {
            "name": {"type": "string"}, "description": {"type": "string"},
            "duration_min": {"type": "integer"}, "price_cents": {"type": "integer"},
            "availability": {"type": "string"},
        },
        "required": ["name"],
    },
    "list_bookings": {"type": "object", "properties": {}, "required": []},
    "analytics_overview": {"type": "object", "properties": {}, "required": []},
    "creator_analytics": {"type": "object", "properties": {}, "required": []},
    "referral_stats": {
        "type": "object",
        "properties": {"contact_id": {"type": "string"}},
        "required": ["contact_id"],
    },
    "creator_network_embed": {"type": "object", "properties": {}, "required": []},
    "create_sponsorship": {
        "type": "object",
        "properties": {
            "sponsor_name": {"type": "string"}, "sponsor_url": {"type": "string"},
            "ad_copy": {"type": "string"}, "price_cents": {"type": "integer"},
        },
        "required": ["sponsor_name"],
    },
}


def mistral_tool_schemas() -> list[dict]:
    return [
        {"type": "function", "function": {"name": name, "description": _description(name),
                                          "parameters": PARAMETERS[name]}}
        for name in TOOLS
    ]


DESCRIPTIONS = {
    "get_service_stats": "Dashboard totals: audiences, contacts, subscribed, campaigns, opens, clicks.",
    "list_audiences": "List all audiences with contact counts.",
    "create_audience": "Create a new audience (mailing list).",
    "list_contacts": "Search/list contacts, optionally filtered by audience, status, tag or text search.",
    "add_contact": "Add or update a subscriber in an audience.",
    "remove_contact": "Delete a contact by id.",
    "tag_contact": "Add tags to an existing contact.",
    "unsubscribe_contact": "Mark a contact as unsubscribed by email address.",
    "list_campaigns": "List campaigns with id, name, subject and status.",
    "get_campaign": "Fetch one campaign including content and recipient count.",
    "create_campaign": "Create a draft campaign with HTML content for an audience.",
    "update_campaign": "Edit a draft campaign's fields.",
    "send_campaign": "Send a draft campaign. Use dry_run=true first to preview the recipient count.",
    "send_campaign_preview": "Send a test copy of a campaign to one email address.",
    "get_campaign_stats": "Opens, clicks and rates for a campaign.",
    "send_test_email": "Send an arbitrary ad-hoc HTML email (requires SMTP configured).",
    "import_mailchimp_data": "Import all audiences, contacts, tags and campaigns from a Mailchimp account using its API key.",
    "list_tags": "List all tags and their contact counts.",
    "create_template": "Save a reusable HTML email template.",
    "create_sequence": "Create an automated sequence: trigger_type signup (welcome), tag_added or purchased.",
    "add_sequence_step": "Append an email step to a sequence at a position with delay_hours before it.",
    "activate_sequence": "Activate a sequence so triggers start enrolling subscribers.",
    "enroll_sequence": "Enroll contacts into a sequence by contact_id, segment_id or tag.",
    "automation_tick": "Process due sequence steps now (normally triggered on a schedule).",
    "create_segment": "Create a dynamic segment from rules (tag/any, event/has, status/eq, email/contains).",
    "preview_segment": "Preview which contacts match a segment right now.",
    "publish_post": "Write and publish a blog/newsletter post (visibility public or paid).",
    "create_landing_page": "Publish a landing page with a subscribe form at /p/{slug}.",
    "create_ab_test": "Create an A/B test with >=2 variants (label, subject, html) for a draft campaign.",
    "send_ab_test": "Send the A/B test: recipients are split across variants automatically.",
    "ab_winner": "Declare the winner of an A/B test by open rate.",
    "create_product": "List a digital product in the shop (price_cents, type download/course/coaching).",
    "list_orders": "List recent orders with amounts.",
    "create_subscription_plan": "Create a paid newsletter tier (monthly or yearly price in cents).",
    "create_service": "Publish a bookable service; availability JSON like {'Mon': ['09:00-17:00']}.",
    "list_bookings": "List upcoming bookings.",
    "analytics_overview": "Full analytics: growth, engagement rates, revenue, funnel.",
    "creator_analytics": "Creator dashboard: subscribers, MRR, revenue, referrals.",
    "referral_stats": "Referral link and signups for one contact.",
    "creator_network_embed": "Get the HTML snippet recommending partner newsletters.",
    "create_sponsorship": "List a sponsorship slot for advertisers with a price.",
}


def _description(name: str) -> str:
    return DESCRIPTIONS.get(name, "")


def run_tool(name: str, arguments: dict) -> Any:
    if name not in TOOLS:
        raise KeyError(f"Unknown tool: {name}")
    return TOOLS[name](arguments or {})


def tool_specs_for(name: str) -> dict:
    return json.dumps({"name": name, "parameters": PARAMETERS.get(name)})


AUDIENCE_STATUS_VALUES = ("subscribed", "unsubscribed", "pending", "cleaned", "transactional")

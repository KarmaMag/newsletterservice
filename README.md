# Newsletter Agent

A self-hosted newsletter platform (Mailchimp/ConvertKit/Beehiiv-class): email
marketing with templates and automated sequences, behavioural automation, dynamic
segments, a landing-page/blog builder, digital product sales, paid subscriptions,
bookings, A/B testing, analytics, referral & sponsorship monetization, webhook
integrations, a web dashboard, and an **AI agent layer** (Mistral tool-calling
agent + MCP server + in-app AI chat). It also ships with a **Mailchimp importer**
that migrates your entire existing account.

## Quick start

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

Open http://localhost:8000 for the admin dashboard. The public site lives at
`/blog`, `/shop`, `/plans`, `/book`, `/p/{slug}` (landing pages), `/f/{form}`.
Data is stored in SQLite (`data/newsletter.db`, override with `NEWSLETTER_DB`).

## Connecting to Mailchimp

1. In Mailchimp: **Account → Profile → Extras → API keys → Create A Key**.
2. Copy the full key including its datacenter suffix, e.g. `xxxxxxxx-us21`.
3. Dashboard → **Mailchimp** tab → paste key → *Import now*, or:
   ```bash
   curl -X POST localhost:8000/api/import/mailchimp \
     -H 'Content-Type: application/json' \
     -d '{"api_key": "xxxxxxxx-us21", "with_activity": true}'
   ```

Imported: audiences, all member statuses, `FNAME`/`LNAME` → names, tags, sent
campaigns with HTML, and optionally open/click history. Idempotent (re-runs sync).

## Feature guide

| Feature | Where |
|---|---|
| **Newsletters & campaigns** | `/api/campaigns`, Campaigns tab, dry-run + real SMTP send |
| **Templates** | Templates tab, `/api/templates` |
| **Automated sequences** | Automations tab: triggers (`signup` = welcome, `tag_added`, `purchased`, `manual`), steps with `delay_hours`, `POST /api/automations/tick` processes due steps (call from cron) |
| **Visual automations** | Automations tab shows the step flow and enrollment state; agent tools can build whole journeys |
| **Segments** | Tag/behaviour/purchase/source rules ANDed dynamically: `{"field":"event","op":"has","value":"purchase"}`, result demos via Form/landing page `source` |
| **Landing pages** | Pages live at `/p/{slug}` with an auto subscribe form; per-variant reports at `POST /api/pages/{id}/ab-report` |
| **Blog / website** | `/blog`, `/blog/{slug}`, paid posts gated with a payment CTA |
| **Signup forms** | `/f/{form_id}` hosted forms with tags + source attribution |
| **Digital products** | `/shop`, `/shop/{slug}` with demo checkout; orders recorded, customer tagged, revenue in analytics |
| **Paid subscriptions** | `/plans` with tiers, active subscribers feed the paid-post gateway and MRR metrics |
| **Bookings** | Services + weekly availability JSON generate slots at `/book/{service}`; double-booking is rejected |
| **A/B testing** | Email: `POST /api/campaigns/{id}/ab-tests` (2+ variants) — recipients hash-split, opens attributed, `POST /api/ab-tests/{id}/winner` declares the result. Landing pages: variants with weights + conversion rate report |
| **Analytics** | `GET /api/analytics/overview` (growth, engagement, revenue, funnel) and `GET /api/analytics/creator` (subscribers, revenue, referrals, network clicks) |
| **Referrals** | Every contact gets a link; `/r/{code}` attribution, leaderboard at `/api/referrals/leaderboard/top` |
| **Creator Network** | Add partner recommendations, get an embed snippet (`POST /api/recommendations/embed`), and click/impression tracking per partner |
| **Sponsorships** | List slots with a price, book/deliver them, revenue summary endpoint |
| **Integrations** | Outbound webhooks with HMAC signing; directory at `/api/integrations/directory` |
| **AI chat** | AI Chat tab → `POST /api/agent/chat`; uses the Mistral agent (with `MISTRAL_API_KEY`) for data/strategy questions, or returns a data snapshot without a key |

## Sending (SMTP)

```bash
export SMTP_HOST=smtp.provider.com SMTP_PORT=587
export SMTP_USER=you@example.com SMTP_PASSWORD=...
export SMTP_FROM_EMAIL=you@example.com SMTP_FROM_NAME="Acme"
export PUBLIC_BASE_URL=https://your-domain    # unsubscribe links
```

Campaign sends append an unsubscribe link and a `campaign.sent` webhook.

## Agent layer

**MCP server** (Claude Desktop, MCP inspector, agent frameworks):
```json
{ "newsletter-agent": { "command": "python", "args": ["-m", "app.agent.mcp_server"] } }
```
41 tools cover contacts, campaigns, templates, sequences, segments, A/B tests,
products/plans, bookings, analytics, referrals, creator network and sponsorships.

**Mistral CLI agent** (service on localhost:8000):
```bash
export MISTRAL_API_KEY=...
python -m app.agent.agent "who are my vip subscribers and what did they buy?"
```

## Tests

```bash
python -m pytest tests -q
```

Tests cover the Mailchimp importer (mocked API), automations (welcome flow,
trigger enrollment, unsubscribe handling), segments, A/B splits & winners, the
paywall, commerce (orders/subscriptions), bookings (slot generation, conflicts),
referrals, sponsorships, webhooks, analytics and AI chat fallback.

## Notes and limits

- Billing/checkout is a **demo provider**: orders and subscriptions are recorded
  and feed analytics, but no real money moves. Swap `commerce.create_order` and
  `commerce.create_subscription` for Stripe checkout calls when ready.
- Sequence delivery is tick-based (`POST /api/automations/tick`); in production
  wire it to a schedule (cron/systemd timer) every 5 minutes.
- SQLite is fine to thousands of subscribers. For larger lists, move to Postgres
  and make `send_campaign` queue-based.
- Open/click tracking is pixel/link based; connect your provider's webhooks to
  `POST /api/campaigns/{id}/track/{open|click|bounce|complaint}` for real
  deliverability signals.

"""Mistral-powered agent loop over the newsletter service tools.

Usage:
    MISTRAL_API_KEY=... python -m app.agent.agent "How many subscribers do I have?"
    MISTRAL_API_KEY=... python -m app.agent.agent   # interactive chat

The service must be running on http://localhost:8000 (uvicorn app.main:app).
"""
import json
import os
import sys

import httpx

from .tools import mistral_tool_schemas, run_tool

API_BASE = os.environ.get("MISTRAL_API_BASE", "https://api.mistral.ai")
MODEL = os.environ.get("AGENT_MODEL", "mistral-large-latest")
MAX_STEPS = 8

SYSTEM_PROMPT = """You are a newsletter service agent managing audiences, contacts, \
campaigns, templates, automated sequences, segments, landing pages, a blog/publication, \
digital products, paid subscriptions, bookings, A/B tests, referrals, sponsorships and \
integrations. You can also import all data from a Mailchimp account.

Rules:
- Check recipient counts with send_campaign(dry_run=true) before any real send.
- Never send the same campaign twice; it is rejected after status becomes 'sent'.
- When asked to import from Mailchimp, ask for the API key if it was not provided. \
Use import_mailchimp_data and summarize the report afterwards.
- When drafting HTML newsletters, produce clean, responsive, table-free-where-possible \
HTML with an unsubscribe line is added automatically by the sender, so do not add one.
- For welcome/follow-up journeys use create_sequence with trigger_type, add steps with \
delays, then activate_sequence.
- For strategy/analytics questions, pull live numbers first (analytics_overview, \
creator_analytics, get_campaign_stats) and ground every claim in them.
- Report results with concrete numbers.
"""


def _chat(messages: list, tools: list) -> dict:
    resp = httpx.post(
        f"{API_BASE}/v1/chat/completions",
        headers={"Authorization": f"Bearer {os.environ['MISTRAL_API_KEY']}"},
        json={"model": MODEL, "messages": messages, "tools": tools,
              "temperature": 0.2},
        timeout=120,
    )
    resp.raise_for_status()
    return resp.json()


def run_agent(user_message: str, history: list | None = None) -> str:
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": user_message})
    tools = mistral_tool_schemas()

    for _ in range(MAX_STEPS):
        data = _chat(messages, tools)
        choice = data["choices"][0]
        message = choice["message"]
        tool_calls = message.get("tool_calls") or []
        if not tool_calls:
            return message["content"] or ""
        messages.append(message)
        for call in tool_calls:
            name = call["function"]["name"]
            try:
                arguments = json.loads(call["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                arguments = {}
            try:
                result = run_tool(name, arguments)
                content = json.dumps(result, default=str)
            except Exception as exc:
                content = json.dumps({"error": str(exc)})
            messages.append({"role": "tool", "name": name, "content": content[:20000]})
    return "Stopped after too many tool steps; ask me to continue."


def main() -> None:
    if not os.environ.get("MISTRAL_API_KEY"):
        print("Set MISTRAL_API_KEY to use the agent.")
        sys.exit(1)
    if len(sys.argv) > 1:
        print(run_agent(" ".join(sys.argv[1:])))
        return
    history = []
    print("Newsletter agent ready. Type 'exit' to quit.")
    while True:
        try:
            user_input = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not user_input:
            continue
        if user_input.lower() in ("exit", "quit"):
            break
        answer = run_agent(user_input, history)
        print(f"agent> {answer}")
        history.append({"role": "user", "content": user_input})
        history.append({"role": "assistant", "content": answer})


if __name__ == "__main__":
    main()

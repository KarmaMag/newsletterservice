"""Thin client for the Mailchimp Marketing API (v3).

Docs: https://mailchimp.com/developer/marketing/api/

Authentication: any username + the API key as the password (HTTP Basic auth).
The API key looks like ``abcd1234-us21``; the "-us21" suffix is the datacenter
prefix used to build the base URL.
"""
import time
from typing import Any, Dict, Iterator, Optional

import httpx


class MailchimpError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def parse_server_prefix(api_key: str) -> str:
    """Extract the datacenter prefix from a Mailchimp API key (e.g. 'us21')."""
    api_key = api_key.strip()
    if "-" not in api_key:
        raise MailchimpError(
            "Invalid Mailchimp API key format: expected a suffix like '-us21'. "
            "Copy the key exactly as shown in your Mailchimp account."
        )
    return api_key.rsplit("-", 1)[1].lower()


class MailchimpClient:
    """Paginated HTTP client for the Mailchimp Marketing API."""

    PAGE_SIZE = 1000  # Mailchimp's maximum page size

    def __init__(self, api_key: str, base_url: Optional[str] = None,
                 timeout: float = 30.0, max_retries: int = 3):
        self.api_key = api_key.strip()
        try:
            self.server = parse_server_prefix(self.api_key)
        except MailchimpError:
            if base_url:
                self.server = "custom"
            else:
                raise
        self.base_url = (
            base_url.rstrip("/") if base_url
            else f"https://{self.server}.api.mailchimp.com/3.0"
        )
        self._max_retries = max_retries
        self._client = httpx.Client(
            auth=(("anystring", self.api_key)),
            timeout=timeout,
            headers={"Accept": "application/json", "User-Agent": "newsletter-agent/1.0"},
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def request(self, method: str, path: str, params: Optional[dict] = None,
                json_body: Optional[dict] = None) -> Any:
        last_error: Optional[Exception] = None
        # clients hitting rate limits or transient errors are retried with backoff
        for attempt in range(self._max_retries):
            try:
                resp = self._client.request(
                    method, f"{self.base_url}{path}", params=params, json=json_body
                )
                if resp.status_code == 429 or resp.status_code >= 500:
                    last_error = MailchimpError(
                        f"Mailchimp returned HTTP {resp.status_code}", resp.status_code
                    )
                elif resp.status_code >= 400:
                    detail = resp.json().get("detail", resp.text) if resp.content else resp.text
                    raise MailchimpError(detail, resp.status_code)
                else:
                    return resp.json() if resp.content else {}
            except httpx.HTTPError as exc:
                last_error = exc
            if attempt < self._max_retries - 1:
                time.sleep(min(2 ** attempt, 10))
        raise MailchimpError(f"Mailchimp request failed after {self._max_retries} attempts: {last_error}")

    def get(self, path: str, params: Optional[dict] = None) -> Any:
        return self.request("GET", path, params=params)

    def post(self, path: str, json_body: Optional[dict] = None) -> Any:
        return self.request("POST", path, json_body=json_body)

    def iter_pages(self, path: str, params: Optional[dict] = None,
                   list_key: str = "members") -> Iterator[dict]:
        """Yield items across all pages of a Mailchimp list endpoint."""
        params = dict(params or {})
        params.setdefault("count", self.PAGE_SIZE)
        offset = 0
        total: Optional[int] = None
        while True:
            params["offset"] = offset
            data = self.get(path, params=params)
            items = data.get(list_key, [])
            if total is None:
                total = int(data.get("total_items", 0))
            for item in items:
                yield item
            offset += len(items)
            if not items or offset >= (total or 0):
                return

    # ---- convenience endpoints -------------------------------------------

    def ping(self) -> dict:
        return self.get("/ping")

    def list_audiences(self) -> Iterator[dict]:
        return self.iter_pages("/lists", list_key="lists")

    def list_members(self, list_id: str, since: Optional[str] = None) -> Iterator[dict]:
        params = {"status": "subscribed,unsubscribed,cleaned,pending,transactional"}
        if since:
            params["since_last_changed"] = since
        return self.iter_pages(f"/lists/{list_id}/members", params=params, list_key="members")

    def list_campaigns(self, list_id: Optional[str] = None) -> Iterator[dict]:
        params = {"sort_field": "send_time", "sort_dir": "DESC"}
        if list_id:
            params["list_id"] = list_id
        return self.iter_pages("/campaigns", params=params, list_key="campaigns")

    def campaign_content(self, campaign_id: str) -> dict:
        return self.get(f"/campaigns/{campaign_id}/content")

    def member_activity(self, list_id: str, member_id: str) -> Iterator[dict]:
        return self.iter_pages(
            f"/lists/{list_id}/members/{member_id}/activity",
            list_key="activity",
        )


def status_to_internal(mailchimp_status: str) -> str:
    """Map Mailchimp member status to our internal status vocabulary."""
    mapping = {
        "subscribed": "subscribed",
        "unsubscribed": "unsubscribed",
        "cleaned": "cleaned",
        "pending": "pending",
        "double_optin": "pending",
        "transactional": "transactional",
    }
    status = (mailchimp_status or "subscribed").lower()
    return mapping.get(status, "subscribed")

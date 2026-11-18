"""Tests for the Mailchimp client and importer using a fake HTTP transport."""
import pytest

from app.importer import ImportReport, MailchimpClient, MailchimpError, import_all
from app.importer.importer import (_parse_mailchimp_time, _member_names,
                                   status_to_internal)
from app import db


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test.db"))
    db.init_db()
    return db


def make_client(handler, base_url="https://us21.api.mailchimp.com/3.0"):
    import httpx

    def transport_handler(request):
        path = request.url.path.replace("/3.0", "", 1)
        return httpx.Response(200, json=handler(path, request.headers), request=request)

    client = MailchimpClient("fakekey-abc-us21", base_url=base_url)
    client._client = httpx.Client(
        auth=("anystring", "fakekey-abc-us21"),
        transport=httpx.MockTransport(transport_handler),
    )
    return client


def patch_client(monkeypatch, handler):
    import app.importer.importer as imp

    monkeypatch.setattr(imp, "MailchimpClient", lambda api_key, base_url=None, **kw:
                        make_client(handler, base_url=base_url or "https://us21.api.mailchimp.com/3.0"))


FAKE_ACCOUNT = {
    "/lists": {
        "total_items": 1,
        "lists": [{
            "id": "list1",
            "name": "Subscribers",
            "stats": {"member_count": 2},
        }],
    },
    "/lists/list1/members": {
        "total_items": 2,
        "members": [
            {
                "id": "hash_1",
                "email_address": "alice@example.com",
                "status": "subscribed",
                "merge_fields": {"FNAME": "Alice", "LNAME": "Smith"},
                "tags": [{"id": 1, "name": "vip"}],
            },
            {
                "id": "hash_2",
                "email_address": "bob@example.com",
                "status": "unsubscribed",
                "merge_fields": {},
                "tags": [],
            },
        ],
    },
    "/campaigns": {
        "total_items": 1,
        "campaigns": [{
            "id": "camp1",
            "list_id": "list1",
            "status": "sent",
            "send_time": "2024-03-01T10:00:00+00:00",
            "settings": {"title": "Launch", "subject_line": "We are live!",
                         "from_name": "Acme"},
            "from_email": "hello@acme.com",
        }],
    },
    "/campaigns/camp1/content": {"html": "<p>Hello</p>"},
}


def handler(path, headers):
    path = path.rstrip("/")
    if path in FAKE_ACCOUNT:
        return FAKE_ACCOUNT[path]
    raise MailchimpError(f"Unhandled: {path}", 404)


def test_parse_server_prefix():
    client = MailchimpClient("abc123-us21")
    assert client.base_url == "https://us21.api.mailchimp.com/3.0"


def test_parse_server_prefix_invalid():
    with pytest.raises(MailchimpError):
        MailchimpClient("notanapikey")


def test_status_mapping():
    assert status_to_internal("subscribed") == "subscribed"
    assert status_to_internal("UNSUBSCRIBED") == "unsubscribed"
    assert status_to_internal("pending") == "pending"
    assert status_to_internal("unknown") == "subscribed"


def test_member_names_from_merge_fields():
    first, last = _member_names({"merge_fields": {"FNAME": "A", "LNAME": "B"}})
    assert (first, last) == ("A", "B")
    assert _member_names({}) == (None, None)


def test_parse_time():
    assert _parse_mailchimp_time("2024-03-01T10:00:00+00:00") > 0
    assert _parse_mailchimp_time(None) == 0.0


def test_import_all_end_to_end(monkeypatch):
    import app.importer.importer as imp

    patch_client(monkeypatch, handler)
    report = import_all("fakekey-abc-us21")

    assert report["audiences_imported"] == 1
    assert report["contacts_imported"] == 2
    assert report["campaigns_imported"] == 1

    with db.get_db() as conn:
        contacts = {r["email"]: r for r in conn.execute("SELECT * FROM contacts")}
        assert set(contacts) == {"alice@example.com", "bob@example.com"}
        assert contacts["alice@example.com"]["status"] == "subscribed"
        assert contacts["alice@example.com"]["mailchimp_member_id"] == "hash_1"
        assert contacts["bob@example.com"]["status"] == "unsubscribed"

        tags = [r["name"] for r in conn.execute("SELECT name FROM tags")]
        assert tags == ["vip"]

        audience = conn.execute("SELECT * FROM audiences").fetchone()
        assert audience["name"] == "Subscribers"
        assert audience["mailchimp_list_id"] == "list1"

        campaign = conn.execute("SELECT * FROM campaigns").fetchone()
        assert campaign["subject"] == "We are live!"
        assert campaign["status"] == "sent"
        assert campaign["mailchimp_campaign_id"] == "camp1"
        assert "Hello" in campaign["html_content"]


def test_import_is_idempotent(monkeypatch):
    import app.importer.importer as imp

    patch_client(monkeypatch, handler)
    import_all("fakekey-abc-us21")
    report = ImportReport()
    with db.get_db() as conn:
        assert conn.execute("SELECT COUNT(*) c FROM contacts").fetchone()["c"] == 2
    imp.import_all("fakekey-abc-us21")
    with db.get_db() as conn:
        assert conn.execute("SELECT COUNT(*) c FROM contacts").fetchone()["c"] == 2
        assert conn.execute("SELECT COUNT(*) c FROM campaigns").fetchone()["c"] == 1


def test_campaign_without_matching_audience_records_error(monkeypatch):
    import app.importer.importer as imp

    def bad_handler(path, headers):
        if "/campaigns" in path and path.rstrip("/") == "/campaigns":
            return {"total_items": 1, "campaigns": [
                {"id": "c1", "list_id": "unknown_list", "settings": {}, "send_time": None}
            ]}
        return {"total_items": 0, "lists": [], "members": []}

    patch_client(monkeypatch, bad_handler)
    report = ImportReport()
    client = imp.MailchimpClient("fakekey-abc-us21")
    try:
        imp.import_campaigns(client, report, default_audience_id=None)
    finally:
        client.close()
    assert report.campaigns == 0
    assert report.errors, "expected an error for unmatched audience"

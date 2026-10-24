import httpx
import pytest

from confluence_mirror.client import (
    AuthenticationError,
    ConfluenceClient,
    ConfluenceError,
    ForbiddenError,
    NotFoundError,
)
from confluence_mirror.config import ConfluenceSettings
from tests.fake_confluence import TOKEN, FakeConfluence, demo_space


def make(fake, **kw):
    sleeps = []
    settings = ConfluenceSettings(base_url=fake.base_url, page_size=2, max_retries=3, **kw)
    return ConfluenceClient(settings, TOKEN, transport=fake.transport, sleep=sleeps.append), sleeps


@pytest.mark.parametrize(
    "base", ["https://confluence.example.com", "https://confluence.example.com/wiki"]
)
def test_pagination_follows_next_links(base):
    fake = FakeConfluence(base)
    demo_space(fake, "<p>x</p>")
    client, _ = make(fake)
    ids = [c["id"] for c in client.list_content("DEMO")]
    assert ids == ["1001", "1002", "1003", "1004", "1005"]
    assert fake.count(r"/rest/api/content$") == 3  # 5 items, page size 2
    assert all(r.headers["Authorization"] == f"Bearer {TOKEN}" for r in fake.requests)
    assert {r.method for r in fake.requests} == {"GET"}


def test_cross_origin_next_link_refused():
    def handler(request):
        return httpx.Response(
            200,
            json={"results": [{"id": "1"}], "_links": {"next": "https://evil.example.net/steal"}},
        )

    settings = ConfluenceSettings(base_url="https://confluence.example.com")
    client = ConfluenceClient(settings, TOKEN, transport=httpx.MockTransport(handler))
    with pytest.raises(ConfluenceError, match="another origin"):
        list(client.paginate("/rest/api/content"))


def test_pagination_loop_detected():
    def handler(request):
        return httpx.Response(200, json={"results": [], "_links": {"next": "/rest/api/x?start=1"}})

    client = ConfluenceClient(
        ConfluenceSettings(base_url="https://c.example.com"),
        TOKEN,
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(ConfluenceError, match="loop"):
        list(client.paginate("/rest/api/x"))


def test_retries_then_succeeds_honouring_retry_after():
    fake = FakeConfluence()
    demo_space(fake, "<p>x</p>")
    fake.fail(r"/rest/api/space/DEMO$", 503, 429)
    client, sleeps = make(fake)
    assert client.get_space("DEMO")["key"] == "DEMO"
    assert sleeps == [0.0, 0.0]  # Retry-After: 0 from the fake


def test_exponential_backoff_without_retry_after():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(502) if len(calls) < 3 else httpx.Response(200, json={"ok": 1})

    sleeps = []
    client = ConfluenceClient(
        ConfluenceSettings(base_url="https://c.example.com", backoff_base=2),
        TOKEN,
        transport=httpx.MockTransport(handler),
        sleep=sleeps.append,
    )
    assert client.get_json("/rest/api/x") == {"ok": 1}
    assert sleeps == [2.0, 4.0]


def test_gives_up_after_max_retries():
    fake = FakeConfluence()
    demo_space(fake, "<p>x</p>")
    fake.fail(r"/rest/api/space/DEMO$", 500, 500, 500, 500, 500)
    client, sleeps = make(fake)
    with pytest.raises(ConfluenceError) as info:
        client.get_space("DEMO")
    assert info.value.status == 500
    assert len(sleeps) == 3


def test_transport_errors_retried():
    attempts = []

    def handler(request):
        attempts.append(1)
        if len(attempts) == 1:
            raise httpx.ConnectError("connection reset")
        return httpx.Response(200, json={"ok": True})

    client = ConfluenceClient(
        ConfluenceSettings(base_url="https://c.example.com"),
        TOKEN,
        transport=httpx.MockTransport(handler),
        sleep=lambda s: None,
    )
    assert client.get_json("/rest/api/x") == {"ok": True}


@pytest.mark.parametrize(
    "status,exc",
    [
        (401, AuthenticationError),
        (302, AuthenticationError),
        (403, ForbiddenError),
        (404, NotFoundError),
    ],
)
def test_error_mapping(status, exc):
    def handler(request):
        return httpx.Response(
            status, json={"message": "nope"}, headers={"Location": "https://sso.example.com/login"}
        )

    client = ConfluenceClient(
        ConfluenceSettings(base_url="https://c.example.com"),
        TOKEN,
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(exc) as info:
        client.get_json("/rest/api/x")
    assert TOKEN not in str(info.value)


def test_wrong_token_is_auth_error():
    fake = FakeConfluence()
    settings = ConfluenceSettings(base_url=fake.base_url)
    client = ConfluenceClient(settings, "wrong-token", transport=fake.transport)
    with pytest.raises(AuthenticationError):
        client.current_user()


def test_url_for_context_paths():
    client = ConfluenceClient(
        ConfluenceSettings(base_url="https://c.example.com/wiki"),
        TOKEN,
        transport=httpx.MockTransport(lambda r: httpx.Response(200)),
    )
    assert client.url_for("/rest/api/x") == "https://c.example.com/wiki/rest/api/x"
    assert client.url_for("/wiki/rest/api/x") == "https://c.example.com/wiki/rest/api/x"
    assert client.url_for("https://c.example.com/wiki/a") == "https://c.example.com/wiki/a"


def test_client_has_no_write_methods():
    public = {n for n in dir(ConfluenceClient) if not n.startswith("_")}
    assert not public & {"post", "put", "patch", "delete", "create", "update"}


def test_download_streams_atomically(tmp_path):
    fake = FakeConfluence()
    demo_space(fake, "<p>x</p>")
    client, _ = make(fake)
    dest = tmp_path / "a" / "diagram.png"
    digest = client.download("/download/attachments/1001/diagram.png?version=1", dest)
    assert dest.read_bytes() == b"\x89PNG"
    assert len(digest) == 64
    assert not list(dest.parent.glob(".*.part"))

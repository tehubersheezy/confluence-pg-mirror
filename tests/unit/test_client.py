import httpx
import pytest

from confluence_mirror.client import (
    AuthenticationError,
    ConfluenceClient,
    ConfluenceError,
    ForbiddenError,
    NotFoundError,
    cql_string,
)
from confluence_mirror.config import ConfluenceSettings
from tests.fake_confluence import TOKEN, FakeConfluence, demo_space


def make(fake, **kw):
    sleeps = []
    kw.setdefault("max_requests_per_second", 0)  # keep `sleeps` to retry back-off only
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
        ConfluenceSettings(
            base_url="https://c.example.com", backoff_base=2, max_requests_per_second=0
        ),
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


def test_throttle_spaces_requests_and_counts_them():
    fake = FakeConfluence()
    demo_space(fake, "<p>x</p>")
    now = [100.0]
    sleeps = []

    def sleep(s):
        sleeps.append(round(s, 6))
        now[0] += s

    settings = ConfluenceSettings(base_url=fake.base_url, max_requests_per_second=4)
    client = ConfluenceClient(
        settings, TOKEN, transport=fake.transport, sleep=sleep, clock=lambda: now[0]
    )
    for _ in range(3):
        client.get_content(1001, expand="version")
    assert sleeps == [0.25, 0.25]
    assert client.request_counts == {"/rest/api/content/{id}": 3}

    unthrottled = ConfluenceClient(
        ConfluenceSettings(base_url=fake.base_url, max_requests_per_second=0),
        TOKEN,
        transport=fake.transport,
        sleep=sleep,
        clock=lambda: now[0],
    )
    sleeps.clear()
    unthrottled.get_content(1001, expand="version")
    assert sleeps == []


def test_content_by_ids_and_cql_quoting():
    fake = FakeConfluence()
    demo_space(fake, "<p>x</p>")
    client, _ = make(fake)
    got = [c["id"] for c in client.content_by_ids([1003, 1001], "version")]
    assert sorted(got) == ["1001", "1003"]
    assert fake.count(r"/content/search$") == 1  # limit defaults to the batch size
    assert cql_string('a "b" \\c') == '"a \\"b\\" \\\\c"'


@pytest.mark.parametrize("style", ["cursor", "link"])
def test_scan_pagination(style):
    fake = FakeConfluence()
    demo_space(fake, "<p>x</p>")
    fake.scan_style = style
    client, _ = make(fake)
    ids = [c["id"] for c in client.scan_content("DEMO", "page", "version", limit=2)]
    assert ids == ["1001", "1002", "1003", "1004", "1005"]
    assert fake.count(r"/content/scan$") == 3
    assert all(
        r.url.params.get("spaceKey") == "DEMO" for r in fake.requests if "cursor" in str(r.url)
    )


def test_scan_repeated_cursor_is_an_error():
    def handler(request):
        return httpx.Response(200, json={"results": [{"id": "1"}], "limit": 1, "nextCursor": "c1"})

    settings = ConfluenceSettings(base_url="https://confluence.example.com")
    client = ConfluenceClient(settings, TOKEN, transport=httpx.MockTransport(handler))
    with pytest.raises(ConfluenceError, match="loop"):
        list(client.scan_content("X", "page", "version", limit=1))


def test_429_halves_the_rate_for_the_rest_of_the_run():
    fake = FakeConfluence()
    demo_space(fake, "<p>x</p>")
    fake.fail(r"/rest/api/content/1001$", 429, 429, 503)
    client, sleeps = make(fake)  # unlimited until rate-limited
    client.get_content(1001, expand="version")
    assert client.rate == 1.0  # 0 (none) -> 2.0 -> 1.0; a 503 does not slow down
    assert client.retry_counts == {"429": 2, "503": 1}
    assert client.request_counts == {"/rest/api/content/{id}": 4}

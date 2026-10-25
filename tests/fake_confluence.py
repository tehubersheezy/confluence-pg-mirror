"""An in-memory, synthetic Confluence Data Center REST API for tests.

It implements just the read-only endpoints the mirror uses, with DC-style pagination
(``start``/``limit`` and ``_links.next`` relative to ``_links.base``). Only GET is served;
any other method fails the test, which is how the read-only guarantee is asserted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from urllib.parse import unquote, urlencode, urlsplit

import httpx

TOKEN = "synthetic-test-pat-0123456789"
EPOCH = datetime(2024, 1, 1, tzinfo=UTC)


def _ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


@dataclass
class FakeAttachment:
    id: str
    filename: str
    media_type: str = "application/octet-stream"
    data: bytes = b"synthetic"
    version: int = 1
    modified: datetime = EPOCH


@dataclass
class FakePage:
    id: int
    space_key: str
    title: str
    parent_id: int | None
    body: str
    type: str = "page"
    version: int = 1
    status: str = "current"  # current | trashed | purged
    labels: list[str] = field(default_factory=list)
    properties: dict[str, object] = field(default_factory=dict)
    attachments: list[FakeAttachment] = field(default_factory=list)
    read_users: list[str] = field(default_factory=list)
    read_groups: list[str] = field(default_factory=list)
    update_groups: list[str] = field(default_factory=list)
    modified: datetime = EPOCH
    position: int | None = None


class FakeConfluence:
    def __init__(self, base_url: str = "https://confluence.example.com") -> None:
        self.base_url = base_url.rstrip("/")
        self.context = urlsplit(self.base_url).path
        self.spaces: dict[str, dict] = {}
        self.space_props: dict[str, dict[str, object]] = {}
        self.pages: dict[int, FakePage] = {}
        self.requests: list[httpx.Request] = []
        # path regex -> list of status codes to return (consumed one per matching request)
        self.failures: list[tuple[re.Pattern[str], list[int]]] = []
        self.hide_from_listing: set[int] = set()
        # /content/scan behaviour: "cursor" (nextCursor only), "link" (also _links.next) or
        # None (an older server without the endpoint: 404).
        self.scan_style: str | None = "cursor"
        # Page size of embedded collections (labels, children.attachment) in expansions.
        self.expansion_limit = 25
        self.clock = EPOCH
        self.transport = httpx.MockTransport(self.handle)

    # -- fixture building ------------------------------------------------------------

    def add_space(
        self,
        key: str,
        space_id: int,
        name: str | None = None,
        homepage: int | None = None,
        properties: dict | None = None,
    ) -> None:
        self.spaces[key] = {
            "id": space_id,
            "key": key,
            "name": name or f"{key} space",
            "type": "global",
            "status": "current",
            "homepage": homepage,
            "description": f"Synthetic {key} space",
        }
        self.space_props[key] = dict(properties or {})

    def add_page(
        self,
        page_id: int,
        space_key: str,
        title: str,
        parent_id: int | None = None,
        body: str = "<p>synthetic</p>",
        **kwargs,
    ) -> FakePage:
        page = FakePage(page_id, space_key, title, parent_id, body, modified=self.tick(), **kwargs)
        self.pages[page_id] = page
        return page

    def tick(self) -> datetime:
        """Monotonic modification time that never lags the real clock (the sync cursor
        comes from the database's now())."""
        self.clock = max(self.clock + timedelta(minutes=5), datetime.now(UTC))
        return self.clock

    def edit(self, page_id: int, *, body: str | None = None, title: str | None = None) -> None:
        page = self.pages[page_id]
        page.version += 1
        page.modified = self.tick()
        if body is not None:
            page.body = body
        if title is not None:
            page.title = title

    def fail(self, path_regex: str, *statuses: int) -> None:
        self.failures.append((re.compile(path_regex), list(statuses)))

    def count(self, path_regex: str, *, param: tuple[str, str] | None = None) -> int:
        rx = re.compile(path_regex)
        n = 0
        for r in self.requests:
            if rx.search(r.url.path) and (
                param is None or param[1] in r.url.params.get(param[0], "")
            ):
                n += 1
        return n

    # -- JSON shapes ------------------------------------------------------------------

    def _ancestors(self, page: FakePage) -> list[FakePage]:
        chain: list[FakePage] = []
        parent = page.parent_id
        while parent is not None and parent in self.pages:
            chain.append(self.pages[parent])
            parent = self.pages[parent].parent_id
        return list(reversed(chain))

    def _user(self, name: str) -> dict:
        return {
            "type": "known",
            "username": name,
            "userKey": f"key-{name}",
            "displayName": name.title(),
        }

    def _restriction_block(self, page: FakePage) -> dict:
        def op(name, users, groups):
            return {
                "operation": name,
                "restrictions": {
                    "user": {
                        "results": [self._user(u) for u in users],
                        "size": len(users),
                        "start": 0,
                        "limit": 200,
                    },
                    "group": {
                        "results": [{"type": "group", "name": g} for g in groups],
                        "size": len(groups),
                        "start": 0,
                        "limit": 200,
                    },
                },
            }

        return {
            "read": op("read", page.read_users, page.read_groups),
            "update": op("update", [], page.update_groups),
        }

    def content_json(self, page: FakePage, expand: str) -> dict:
        space = self.spaces[page.space_key]
        data: dict = {
            "id": str(page.id),
            "type": page.type,
            "status": page.status,
            "title": page.title,
            "space": {"id": space["id"], "key": space["key"], "name": space["name"]},
            "version": {
                "number": page.version,
                "when": _ts(page.modified),
                "message": "",
                "minorEdit": False,
                "by": self._user("editor"),
            },
            "history": {"createdDate": _ts(EPOCH), "createdBy": self._user("author")},
            "ancestors": [
                {"id": str(a.id), "type": "page", "title": a.title} for a in self._ancestors(page)
            ],
            "extensions": {"position": page.position if page.position is not None else "none"},
            "metadata": {
                "labels": self._collection(
                    [
                        {"prefix": "global", "name": n, "id": str(abs(hash(n)) % 10**6)}
                        for n in page.labels
                    ],
                    f"/rest/api/content/{page.id}/label",
                )
            },
            "_links": {
                "webui": f"/pages/viewpage.action?pageId={page.id}",
                "tinyui": f"/x/{page.id}",
            },
        }
        bodies = {
            "storage": page.body,
            "view": f"<div class='view'>{page.body}</div>",
            "export_view": f"<div class='export'>{page.body}</div>",
        }
        wanted = {r for r in bodies if f"body.{r}" in expand.split(",")}
        if wanted:
            data["body"] = {r: {"value": bodies[r], "representation": r} for r in sorted(wanted)}
        if "restrictions" in expand:
            data["restrictions"] = self._restriction_block(page)
        parts = expand.split(",")
        if any(x.startswith("children.attachment") for x in parts):
            data["children"] = {
                "attachment": self._collection(
                    [self.attachment_json(page, a) for a in page.attachments],
                    f"/rest/api/content/{page.id}/child/attachment",
                )
            }
        keys = [x.split(".", 2)[2] for x in parts if x.startswith("metadata.properties.")]
        if keys:
            data["metadata"]["properties"] = {
                k: {"id": "1", "key": k, "value": page.properties[k], "version": {"number": 1}}
                for k in keys
                if k in page.properties
            }
        return data

    def _collection(self, items: list, next_path: str) -> dict:
        """An embedded collection as DC returns it: the first page, with a next link."""
        limit = self.expansion_limit
        block: dict = {
            "results": items[:limit],
            "start": 0,
            "limit": limit,
            "size": len(items[:limit]),
            "_links": {},
        }
        if len(items) > limit:
            block["_links"]["next"] = f"{next_path}?start={limit}&limit={limit}"
        return block

    def attachment_json(self, page: FakePage, att: FakeAttachment) -> dict:
        return {
            "id": att.id,
            "type": "attachment",
            "status": "current",
            "title": att.filename,
            "version": {"number": att.version, "when": _ts(att.modified)},
            "history": {"createdDate": _ts(EPOCH), "createdBy": self._user("author")},
            "container": {"id": str(page.id), "type": page.type, "title": page.title},
            "metadata": {"mediaType": att.media_type, "comment": "synthetic attachment"},
            "extensions": {
                "mediaType": att.media_type,
                "fileSize": len(att.data),
                "comment": "synthetic attachment",
            },
            "_links": {
                "download": f"/download/attachments/{page.id}/{att.filename}"
                f"?version={att.version}&api=v2"
            },
        }

    def _cql(self, cql: str, expand: str) -> list[dict]:
        """A CQL subset: clauses joined by AND over id, type, space, parent, ancestor, title~
        and lastmodified (attachments)."""
        clauses = [c.strip() for c in re.split(r"\s+and\s+", cql.strip(), flags=re.I)]
        clauses = [
            c[1:-1].strip() if c.startswith("(") and c.endswith(")") and "(" not in c[1:-1] else c
            for c in clauses
        ]
        kinds = [m.group(1) for c in clauses if (m := re.fullmatch(r'type\s*=\s*"?(\w+)"?', c))]
        if kinds == ["attachment"]:
            return self._attachment_cql(clauses)
        pages = [p for p in self.pages.values() if p.status == "current"]
        for clause in clauses:
            if m := re.fullmatch(r'type\s*=\s*"?(\w+)"?', clause):
                pages = [p for p in pages if p.type == m.group(1)]
            elif m := re.fullmatch(r"id\s+in\s*\(([\d,\s]+)\)", clause, re.I):
                ids = {int(x) for x in m.group(1).split(",") if x.strip()}
                pages = [p for p in pages if p.id in ids]
            elif m := re.fullmatch(r"id\s*=\s*(\d+)", clause):
                pages = [p for p in pages if p.id == int(m.group(1))]
            elif m := re.fullmatch(r'space\s*=\s*"([^"]+)"', clause):
                pages = [p for p in pages if p.space_key == m.group(1)]
            elif m := re.fullmatch(r"parent\s*=\s*(\d+)", clause):
                pages = [p for p in pages if p.parent_id == int(m.group(1))]
            elif m := re.fullmatch(r"ancestor\s*=\s*(\d+)", clause):
                pages = [p for p in pages if int(m.group(1)) in {a.id for a in self._ancestors(p)}]
            elif m := re.fullmatch(r'title\s*~\s*"([^"]+)"', clause):
                pages = [p for p in pages if m.group(1).lower() in p.title.lower()]
            elif m := re.fullmatch(r'label\s*=\s*"?([\w-]+)"?', clause):
                pages = [p for p in pages if m.group(1) in p.labels]
            else:
                raise AssertionError(f"fake CQL cannot parse {clause!r}")
        return [self.content_json(p, expand) for p in sorted(pages, key=lambda p: p.id)]

    def _attachment_cql(self, clauses: list[str]) -> list[dict]:
        pages = [p for p in self.pages.values() if p.status == "current"]
        since = None
        for clause in clauses:
            if m := re.fullmatch(r'space\s*=\s*"([^"]+)"', clause):
                pages = [p for p in pages if p.space_key == m.group(1)]
            elif m := re.fullmatch(r'lastmodified\s*>=\s*"([^"]+)"', clause):
                since = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M").replace(tzinfo=UTC)
            elif not re.fullmatch(r'type\s*=\s*"?attachment"?', clause):
                raise AssertionError(f"fake CQL cannot parse {clause!r}")
        return [
            self.attachment_json(p, a)
            for p in sorted(pages, key=lambda p: p.id)
            for a in p.attachments
            if since is None or a.modified >= since
        ]

    def _scan(self, request: httpx.Request, expand: str) -> httpx.Response:
        params = request.url.params
        if self.scan_style is None or (
            self.scan_style == "pages-only" and params.get("type") != "page"
        ):
            return httpx.Response(404, json={"message": "unhandled /rest/api/content/scan"})
        limit = int(params.get("limit", 25))
        after = int(params.get("cursor", "content:false:0").rsplit(":", 1)[1] or 0)
        items = sorted(
            (
                p
                for p in self.pages.values()
                if p.space_key == params.get("spaceKey")
                and p.type == params.get("type")
                and p.status == "current"
                and p.id not in self.hide_from_listing
                and p.id > after
            ),
            key=lambda p: p.id,
        )
        chunk = items[:limit]
        body: dict = {
            "results": [self.content_json(p, expand) for p in chunk],
            "limit": limit,
            "size": len(chunk),
            "cursor": params.get("cursor", "content:false:null"),
            "_links": {"base": self.base_url, "context": self.context},
        }
        if len(items) > limit:
            body["nextCursor"] = f"content:false:{chunk[-1].id}"
            if self.scan_style == "link":
                q = dict(params)
                q["cursor"] = body["nextCursor"]
                body["_links"]["next"] = f"/rest/api/content/scan?{urlencode(q)}"
        return httpx.Response(200, json=body)

    # -- HTTP ---------------------------------------------------------------------------

    def _paginate(self, request: httpx.Request, items: list) -> httpx.Response:
        params = dict(request.url.params)
        start = int(params.get("start", 0))
        limit = int(params.get("limit", 25))
        chunk = items[start : start + limit]
        body: dict = {
            "results": chunk,
            "start": start,
            "limit": limit,
            "size": len(chunk),
            "_links": {"base": self.base_url, "context": self.context},
        }
        if start + limit < len(items):
            params["start"] = str(start + limit)
            path = request.url.path
            if self.context and path.startswith(self.context):
                path = path[len(self.context) :]
            body["_links"]["next"] = f"{path}?{urlencode(params)}"
        return httpx.Response(200, json=body)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.method == "GET", f"write attempted: {request.method} {request.url}"
        if request.headers.get("Authorization") != f"Bearer {TOKEN}":
            return httpx.Response(401, json={"message": "Unauthorized"})
        path = request.url.path
        for rx, statuses in self.failures:
            if statuses and rx.search(path):
                return httpx.Response(
                    statuses.pop(0),
                    json={"message": "injected failure"},
                    headers={"Retry-After": "0"},
                )
        if self.context:
            assert path.startswith(self.context + "/"), f"context path dropped: {path}"
            path = path[len(self.context) :]
        params = request.url.params
        expand = params.get("expand", "")

        if path == "/rest/api/user/current":
            return httpx.Response(200, json=self._user("mirror-bot"))
        if m := re.fullmatch(r"/rest/api/space/([^/]+)", path):
            space = self.spaces.get(m.group(1))
            if not space:
                return httpx.Response(404, json={"message": "No space"})
            out = {k: space[k] for k in ("id", "key", "name", "type", "status")}
            out["description"] = {"plain": {"value": space["description"]}}
            if space["homepage"]:
                out["homepage"] = {"id": str(space["homepage"])}
            return httpx.Response(200, json=out)
        if m := re.fullmatch(r"/rest/api/space/([^/]+)/property", path):
            props = self.space_props.get(m.group(1), {})
            items = [
                {"id": str(i + 1), "key": k, "value": v, "version": {"number": 1}}
                for i, (k, v) in enumerate(sorted(props.items()))
            ]
            return self._paginate(request, items)
        if path == "/rest/api/content":
            items = [
                self.content_json(p, expand)
                for p in sorted(self.pages.values(), key=lambda p: p.id)
                if p.space_key == params.get("spaceKey")
                and p.type == params.get("type")
                and p.status == "current"
                and p.id not in self.hide_from_listing
            ]
            return self._paginate(request, items)
        if path == "/rest/api/space":
            items = []
            for key in sorted(self.spaces):
                space = self.spaces[key]
                if params.get("type") and space["type"] != params.get("type"):
                    continue
                out = {k: space[k] for k in ("id", "key", "name", "type", "status")}
                out["description"] = {"plain": {"value": space["description"]}}
                items.append(out)
            return self._paginate(request, items)
        if path == "/rest/api/content/scan":
            return self._scan(request, expand)
        if path == "/rest/api/content/search":
            # Server caps (Atlassian KB): 50 with a body expansion, 200 with others, 1000 bare.
            cap = 50 if "body." in expand else 200 if expand else 1000
            if int(params.get("limit", 25)) > cap:
                request.url = request.url.copy_merge_params({"limit": str(cap)})
            return self._paginate(request, self._cql(unquote(params.get("cql", "")), expand))
        if m := re.fullmatch(r"/rest/api/content/(\d+)(/.*)?", path):
            page = self.pages.get(int(m.group(1)))
            sub = m.group(2) or ""
            if page is None or page.status == "purged":
                return httpx.Response(404, json={"message": "No content"})
            if page.status == "trashed" and params.get("status") not in ("any", "trashed"):
                return httpx.Response(404, json={"message": "No content"})
            if sub == "":
                return httpx.Response(200, json=self.content_json(page, expand))
            if sub == "/label":
                return self._paginate(
                    request,
                    [
                        {"prefix": "global", "name": n, "id": str(i)}
                        for i, n in enumerate(page.labels)
                    ],
                )
            if sub == "/property":
                return self._paginate(
                    request,
                    [
                        {"id": str(i), "key": k, "value": v, "version": {"number": 1}}
                        for i, (k, v) in enumerate(sorted(page.properties.items()))
                    ],
                )
            if sub == "/child/page":
                children = sorted(
                    (
                        c
                        for c in self.pages.values()
                        if c.parent_id == page.id and c.status == "current"
                    ),
                    key=lambda c: c.id,
                )
                return self._paginate(request, [self.content_json(c, expand) for c in children])
            if sub == "/child/attachment":
                return self._paginate(
                    request, [self.attachment_json(page, a) for a in page.attachments]
                )
            if sub == "/restriction/byOperation":
                return httpx.Response(200, json=self._restriction_block(page))
        if m := re.fullmatch(r"/download/attachments/(\d+)/([^/]+)", path):
            page = self.pages.get(int(m.group(1)))
            att = next((a for a in page.attachments if a.filename == unquote(m.group(2))), None)
            if att:
                return httpx.Response(200, content=att.data)
        return httpx.Response(404, json={"message": f"unhandled {path}"})


def demo_space(fake: FakeConfluence, storage: str) -> None:
    """A small synthetic space: root -> (child -> grandchild, sibling)."""
    fake.add_space(
        "DEMO", 98001, "Demo Space", homepage=1001, properties={"team": {"name": "Synthetic"}}
    )
    fake.add_space("OTHER", 98002, "Other Space")
    fake.add_page(
        1001,
        "DEMO",
        "Root Page",
        None,
        storage,
        labels=["overview", "root"],
        properties={"owner": {"team": "docs"}},
        attachments=[FakeAttachment("att5001", "diagram.png", "image/png", b"\x89PNG")],
        update_groups=["doc-editors"],
    )
    fake.add_page(
        1002,
        "DEMO",
        "Child Page",
        1001,
        '<p>Back to <ac:link><ri:page ri:content-title="Root Page"/></ac:link></p>',
        labels=["child"],
        read_users=["alice"],
        read_groups=["team-a"],
        position=1,
    )
    fake.add_page(
        1003,
        "DEMO",
        "Grandchild Page",
        1002,
        "<table><tbody><tr><th>K</th><th>V</th></tr><tr><td>a</td><td>1</td></tr></tbody></table>",
        position=0,
    )
    fake.add_page(1004, "DEMO", "Sibling Page", 1001, "<p>Sibling</p>", position=2)
    fake.add_page(
        1005,
        "DEMO",
        "Shared Snippet",
        1001,
        '<ac:structured-macro ac:name="excerpt" ac:schema-version="1">'
        "<ac:rich-text-body><p>Reusable</p></ac:rich-text-body></ac:structured-macro>",
    )
    fake.add_page(2001, "OTHER", "Elsewhere", None, "<p>Other space</p>")

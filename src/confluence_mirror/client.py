"""Read-only Confluence Data Center REST client.

* Bearer PAT authentication; the token is registered for log redaction.
* Only GET is possible: the client has no code path that sends any other method.
* TLS verification is always on (certifi roots, plus an optional corporate CA bundle or
  the OS trust store via ``truststore``).
* Retries 429/5xx/transport errors with exponential backoff, honouring Retry-After.
* Pagination follows ``_links.next`` and refuses to follow links to another origin, so the
  token is never sent anywhere but the configured instance.
"""

from __future__ import annotations

import hashlib
import logging
import ssl
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import certifi
import httpx

from confluence_mirror import __version__
from confluence_mirror.config import ConfluenceSettings
from confluence_mirror.logging_utils import redact, register_secret

log = logging.getLogger(__name__)

RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
MAX_BACKOFF = 60.0

CONTENT_EXPAND_LIST = "version,ancestors,space,history,metadata.labels,extensions.position"
CONTENT_EXPAND_FULL = ",".join(
    [
        "body.storage",
        "body.view",
        "body.export_view",
        "version",
        "ancestors",
        "space",
        "history",
        "extensions.position",
        "restrictions.read.restrictions.user",
        "restrictions.read.restrictions.group",
        "restrictions.update.restrictions.user",
        "restrictions.update.restrictions.group",
    ]
)


class ConfluenceError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(redact(message))
        self.status = status


class NotFoundError(ConfluenceError):
    pass


class ForbiddenError(ConfluenceError):
    pass


class AuthenticationError(ConfluenceError):
    pass


def build_ssl_context(settings: ConfluenceSettings) -> ssl.SSLContext:
    if settings.use_system_truststore:
        try:
            import truststore
        except ImportError as exc:  # pragma: no cover - depends on optional extra
            raise ConfluenceError(
                "use_system_truststore requires: pip install 'confluence-pg-mirror[truststore]'"
            ) from exc
        ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    else:
        ctx = ssl.create_default_context(cafile=certifi.where())
    if settings.ca_bundle:
        ctx.load_verify_locations(cafile=str(Path(settings.ca_bundle).expanduser()))
    # Defensive: these are the defaults, and nothing in this project may relax them.
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    return ctx


class ConfluenceClient:
    def __init__(
        self,
        settings: ConfluenceSettings,
        token: str,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        register_secret(token)
        self.settings = settings
        self.base_url = settings.base_url.rstrip("/")
        parts = urlsplit(self.base_url)
        self._origin = f"{parts.scheme}://{parts.netloc}"
        self._context_path = parts.path.rstrip("/")
        self._sleep = sleep
        kwargs: dict[str, Any] = {}
        if transport is not None:
            kwargs["transport"] = transport
        else:
            kwargs["verify"] = build_ssl_context(settings)
        self._http = httpx.Client(
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "User-Agent": f"confluence-pg-mirror/{__version__}",
            },
            timeout=settings.timeout,
            follow_redirects=False,
            **kwargs,
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> ConfluenceClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- low level -------------------------------------------------------------

    def url_for(self, path_or_url: str) -> str:
        """Resolve an API path or a ``_links`` value against the configured instance."""
        if path_or_url.startswith(("http://", "https://")):
            parts = urlsplit(path_or_url)
            if f"{parts.scheme}://{parts.netloc}" != self._origin:
                raise ConfluenceError(f"refusing to follow link to another origin: {parts.netloc}")
            return path_or_url
        if not path_or_url.startswith("/"):
            path_or_url = "/" + path_or_url
        if self._context_path and path_or_url.startswith(self._context_path + "/"):
            return self._origin + path_or_url
        return self.base_url + path_or_url

    def _get(self, path_or_url: str, params: dict[str, Any] | None, *, stream: bool = False):
        url = self.url_for(path_or_url)
        attempt = 0
        while True:
            try:
                request = self._http.build_request("GET", url, params=params)
                response = self._http.send(request, stream=stream)
            except httpx.TransportError as exc:
                if attempt >= self.settings.max_retries:
                    raise ConfluenceError(
                        f"transport error for {urlsplit(url).path}: {exc}"
                    ) from exc
                delay = self._backoff(attempt, None)
                log.warning(
                    "transport error (%s); retry %d in %.1fs",
                    type(exc).__name__,
                    attempt + 1,
                    delay,
                )
                self._sleep(delay)
                attempt += 1
                continue
            if response.status_code in RETRY_STATUSES and attempt < self.settings.max_retries:
                delay = self._backoff(attempt, response.headers.get("Retry-After"))
                log.warning(
                    "HTTP %d for %s; retry %d in %.1fs",
                    response.status_code,
                    urlsplit(url).path,
                    attempt + 1,
                    delay,
                )
                response.close()
                self._sleep(delay)
                attempt += 1
                continue
            if response.status_code >= 300:
                self._raise_for(response, url)
            return response

    def _backoff(self, attempt: int, retry_after: str | None) -> float:
        if retry_after:
            try:
                return min(max(float(retry_after), 0.0), MAX_BACKOFF)
            except ValueError:
                pass
        return min(self.settings.backoff_base * (2**attempt), MAX_BACKOFF)

    @staticmethod
    def _raise_for(response: httpx.Response, url: str) -> None:
        status = response.status_code
        try:
            response.read()
            body = response.json()
            detail = body.get("message") if isinstance(body, dict) else None
        except Exception:
            detail = None
        finally:
            response.close()
        path = urlsplit(url).path
        message = f"HTTP {status} for GET {path}" + (f": {detail}" if detail else "")
        if 300 <= status < 400:
            raise AuthenticationError(
                message + " (redirect; the PAT may be rejected or SSO is intercepting)", status
            )
        if status == 401:
            raise AuthenticationError(message, status)
        if status == 403:
            raise ForbiddenError(message, status)
        if status == 404:
            raise NotFoundError(message, status)
        raise ConfluenceError(message, status)

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        response = self._get(path, params)
        try:
            return response.json()
        except ValueError as exc:
            raise ConfluenceError(
                f"non-JSON response from {urlsplit(str(response.url)).path}", response.status_code
            ) from exc

    def paginate(self, path: str, params: dict[str, Any] | None = None) -> Iterator[dict]:
        """Yield every item of a paginated ``{"results": [...], "_links": {"next": ...}}``."""
        params = dict(params or {})
        params.setdefault("limit", self.settings.page_size)
        next_ref: str | None = path
        next_params: dict[str, Any] | None = params
        seen_pages: set[str] = set()
        while next_ref:
            page = self.get_json(next_ref, next_params)
            if not isinstance(page, dict) or not isinstance(page.get("results"), list):
                raise ConfluenceError(f"unexpected paginated response from {path}")
            yield from page["results"]
            link = (page.get("_links") or {}).get("next")
            if not link:
                break
            # `next` is relative to `_links.base` (the configured base URL, context path
            # included); url_for also accepts context-prefixed and same-origin absolute links.
            resolved = self.url_for(link)
            if resolved in seen_pages:
                raise ConfluenceError(f"pagination loop detected at {path}")
            seen_pages.add(resolved)
            next_ref, next_params = resolved, None

    # -- endpoints (all read-only) ----------------------------------------------------

    def current_user(self) -> dict:
        return self.get_json("/rest/api/user/current")

    def get_space(self, space_key: str) -> dict:
        return self.get_json(
            f"/rest/api/space/{space_key}", {"expand": "description.plain,homepage"}
        )

    def space_properties(self, space_key: str) -> Iterator[dict]:
        return self.paginate(f"/rest/api/space/{space_key}/property", {"expand": "version"})

    def list_content(self, space_key: str, content_type: str = "page") -> Iterator[dict]:
        return self.paginate(
            "/rest/api/content",
            {
                "spaceKey": space_key,
                "type": content_type,
                "status": "current",
                "expand": CONTENT_EXPAND_LIST,
            },
        )

    def get_content(
        self, content_id: int | str, *, expand: str = CONTENT_EXPAND_FULL, status: str | None = None
    ) -> dict:
        params: dict[str, Any] = {"expand": expand}
        if status:
            params["status"] = status
        return self.get_json(f"/rest/api/content/{content_id}", params)

    def labels(self, content_id: int | str) -> Iterator[dict]:
        return self.paginate(f"/rest/api/content/{content_id}/label")

    def content_properties(self, content_id: int | str) -> Iterator[dict]:
        return self.paginate(f"/rest/api/content/{content_id}/property", {"expand": "version"})

    def attachments(self, content_id: int | str) -> Iterator[dict]:
        return self.paginate(
            f"/rest/api/content/{content_id}/child/attachment",
            {"expand": "version,history,container"},
        )

    def restrictions(self, content_id: int | str) -> dict:
        return self.get_json(
            f"/rest/api/content/{content_id}/restriction/byOperation",
            {"expand": "restrictions.user,restrictions.group"},
        )

    def search(self, cql: str, expand: str | None = None) -> Iterator[dict]:
        params: dict[str, Any] = {"cql": cql}
        if expand:
            params["expand"] = expand
        return self.paginate("/rest/api/content/search", params)

    def download(self, download_path: str, dest: Path) -> str:
        """Stream a binary to ``dest`` atomically; return its SHA-256."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(f".{dest.name}.part")
        digest = hashlib.sha256()
        response = self._get(download_path, None, stream=True)
        try:
            with tmp.open("wb") as fh:
                for chunk in response.iter_bytes():
                    digest.update(chunk)
                    fh.write(chunk)
            tmp.replace(dest)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        finally:
            response.close()
        return digest.hexdigest()

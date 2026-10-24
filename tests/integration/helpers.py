from __future__ import annotations

from confluence_mirror.client import ConfluenceClient
from confluence_mirror.config import ConfluenceSettings, SyncSettings
from confluence_mirror.sync import SpaceSync
from tests.fake_confluence import TOKEN, FakeConfluence


def client_for(fake: FakeConfluence, **kw) -> ConfluenceClient:
    settings = ConfluenceSettings(base_url=fake.base_url, page_size=2, max_retries=2, **kw)
    return ConfluenceClient(settings, TOKEN, transport=fake.transport, sleep=lambda s: None)


def sync(conn, fake, space="DEMO", mode="auto", **settings):
    return SpaceSync(conn, client_for(fake), SyncSettings(**settings)).run(space, mode)


def scalar(conn, query, *params):
    value = conn.execute(query, params or None).fetchone()[0]
    conn.rollback()
    return value


def rows(conn, query, *params):
    result = conn.execute(query, params or None).fetchall()
    conn.rollback()
    return result

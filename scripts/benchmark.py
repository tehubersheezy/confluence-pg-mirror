"""Measure confex against the spec's performance targets (section 28) on synthetic data.

    python scripts/benchmark.py --dsn postgresql:///confex_bench

WARNING: drops and recreates the mirror schemas in the given database. Use a throwaway
database. Reports measured timings; it does not assert that targets were met elsewhere.
"""

from __future__ import annotations

import argparse
import json
import platform
import sys
import time
from contextlib import redirect_stdout
from io import StringIO

from confluence_mirror import confex, db, inspection
from confluence_mirror import repository as repo
from confluence_mirror.structure import parse_document


def reset(conn) -> None:
    for schema in ("pipeline", "review", "extracted", "source", "sync"):
        conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
    conn.commit()
    db.migrate(conn)


def seed(conn, pages: int, rows: int, cols: int) -> str:
    conn.execute(
        "INSERT INTO source.spaces (space_id, space_key, name, sync_enabled) "
        "VALUES (1, 'BENCH', 'Benchmark', true)"
    )
    for i in range(pages):
        pid = 10_000 + i
        parent = None if i == 0 else 10_000 + (i - 1) // 10  # a 10-ary tree
        conn.execute(
            "INSERT INTO source.pages (page_id, space_id, title, status, parent_id, "
            "current_version) VALUES (%s, 1, %s, 'current', %s, 1)",
            (pid, f"Page {i:04d}", parent),
        )
    header = "".join(f"<th>Col {c}</th>" for c in range(cols))
    body_rows = "".join(
        "<tr>" + "".join(f"<td>r{r}c{c}</td>" for c in range(cols)) + "</tr>" for r in range(rows)
    )
    body = f"<h2>Big</h2><table><tr>{header}</tr>{body_rows}</table>"
    conn.execute(
        "INSERT INTO source.page_versions (page_id, version_number, title) "
        "VALUES (10000, 1, 'Page 0000')"
    )
    conn.execute(
        "INSERT INTO source.page_content (page_id, version_number, body_storage) "
        "VALUES (10000, 1, %s)",
        (body,),
    )
    conn.commit()
    return body


def timed(fn, repeat: int = 3) -> float:
    best = float("inf")
    for _ in range(repeat):
        start = time.perf_counter()
        fn()
        best = min(best, time.perf_counter() - start)
    return round(best, 4)


def cli(*argv: str) -> int:
    with redirect_stdout(StringIO()):
        return confex.main(list(argv))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", required=True)
    ap.add_argument("--pages", type=int, default=1000)
    ap.add_argument("--rows", type=int, default=500)
    ap.add_argument("--cols", type=int, default=50)
    args = ap.parse_args()
    import os

    os.environ["CONFLUENCE_MIRROR_DSN"] = args.dsn
    with db.connect(args.dsn) as conn:
        reset(conn)
        body = seed(conn, args.pages, args.rows, args.cols)
    results = {
        "environment": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "machine": platform.machine(),
        },
        "data": {"pages": args.pages, "table": f"{args.rows + 1}x{args.cols}"},
    }
    results["parse_large_table_s"] = timed(
        lambda: parse_document(body, page_id=10000, version=1, representation="storage"), 1
    )
    assert cli("parse", "--page", "10000", "--representations", "storage") == 0
    with db.connect(args.dsn) as conn:
        resolved, table = inspection.resolve_table(conn, "10000:t01", representation="storage")
        assert resolved.parse_mode == "persisted"
        results["inspect_grid_persisted_s"] = timed(
            lambda: inspection.table_view(conn, resolved, table, "grid", bounded=True)
        )
        results["inspect_cell_persisted_s"] = timed(
            lambda: inspection.table_view(conn, resolved, table, "cell", cell="R250C25")
        )
        results["load_artifact_s"] = timed(
            lambda: repo.load_artifact(conn, resolved.artifact["artifact_id"])
        )
    results["cli_tables_show_grid_s"] = timed(
        lambda: cli("tables", "show", "10000:t01", "--representation", "storage", "--view", "grid")
    )
    results["cli_spaces_tree_s"] = timed(lambda: cli("spaces", "tree", "BENCH", "--counts"))
    results["cli_pages_tree_s"] = timed(
        lambda: cli("pages", "tree", "--root", "10000", "--depth", "10")
    )
    print(json.dumps(results, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

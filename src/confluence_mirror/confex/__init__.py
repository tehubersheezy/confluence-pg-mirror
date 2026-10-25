"""``confex``: explore the mirror, inspect document and table structure, and run the
evidence-backed extraction pipeline.

Global options go before the subcommand::

    confex --format json tables show 12345:t02 --view grid

Mirror commands open the database read-only and make no network calls. ``db migrate`` is
the only schema change and ``sync`` the only writer of source captures. Every other
writing command only adds derived, immutable artifacts or append-only events, all built
from the rendered bodies (export view, view); storage format is mirrored but never parsed.
"""

from __future__ import annotations

import sys

from confluence_mirror.confex import core, extract, nav, ops, review, runs, tables, templates

MODULES = [nav, tables, templates, extract, runs, review, ops]


def main(argv: list[str] | None = None) -> int:
    return core.run_main(MODULES, argv)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

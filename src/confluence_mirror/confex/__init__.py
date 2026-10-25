"""``confex``: explore the mirror, inspect document and table structure, and run the
evidence-backed extraction pipeline.

Global options go before the subcommand::

    confex --format json tables show 12345:t02 --representation storage --view grid

Mirror commands open the database read-only and make no network calls. ``db migrate`` is
the only schema change, ``sync`` the only writer of source captures, and ``reextract``
rebuilds the mirror's own ``extracted.*`` rows. Every other writing command only adds
derived, immutable artifacts or append-only events.
"""

from __future__ import annotations

import sys

from confluence_mirror.confex import core, extract, nav, ops, review, runs, tables

MODULES = [nav, tables, extract, runs, review, ops]


def main(argv: list[str] | None = None) -> int:
    return core.run_main(MODULES, argv)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

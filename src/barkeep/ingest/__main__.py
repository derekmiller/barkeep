"""Entry point for ``python -m barkeep.ingest``.

The poller lives in :mod:`barkeep.ingest.imap`, but running that module directly
with ``-m`` executes it twice: the package ``__init__`` imports it eagerly, so
runpy finds ``barkeep.ingest.imap`` already in ``sys.modules`` and warns. A
package-level ``__main__`` avoids the collision.
"""

from __future__ import annotations

from .imap import main

if __name__ == "__main__":
    main()

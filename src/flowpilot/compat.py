"""Platform quirks, kept in one place.

psycopg's async driver cannot run on Windows' default asyncio event loop (ProactorEventLoop); it needs
the SelectorEventLoop. Only the pgvector store uses psycopg, so only then is the loop switched.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any


def backend_options(vector_store: str) -> dict[str, Any]:
    if sys.platform == "win32" and vector_store == "pgvector":
        return {"loop_factory": asyncio.SelectorEventLoop}
    return {}

from __future__ import annotations

import ssl
import urllib.request
from typing import Any

import certifi

TLS_CONTEXT = ssl.create_default_context(cafile=certifi.where())


def open_url(request: Any, timeout: float):
    """Open HTTP(S) URLs with a current, packaged CA trust store."""
    return urllib.request.urlopen(request, timeout=timeout, context=TLS_CONTEXT)

"""Offline resource fetchers for the pinned WeasyPrint API.

Import only when the optional HTML engine is requested. Neither fetcher delegates
resource access to the network/file handlers installed by WeasyPrint.
"""

from __future__ import annotations

import base64
import re

from weasyprint.urls import URLFetcher, URLFetcherResponse


class DenyAllFetcher(URLFetcher):
    def __init__(self):
        super().__init__(allowed_protocols=(), allow_redirects=False)
        self.blocked = set()

    def fetch(self, url, headers=None):
        self.blocked.add(url.split(":", 1)[0])
        raise ValueError("External, local and embedded resources are disabled")


class GeneratedImageFetcher(URLFetcher):
    MAX_ENCODED_BYTES = 32 * 1024 * 1024

    def __init__(self):
        super().__init__(allowed_protocols=(), allow_redirects=False)

    def fetch(self, url, headers=None):
        # Reject oversized strings before running a regex or decoding base64.
        if len(url) > self.MAX_ENCODED_BYTES + len("data:image/jpeg;base64,"):
            raise ValueError("Embedded generated image exceeds its size limit")
        match = re.fullmatch(r"data:(image/(?:png|jpeg));base64,([A-Za-z0-9+/=]+)", url)
        if not match or len(match[2]) > self.MAX_ENCODED_BYTES:
            raise ValueError("Only bounded embedded generated images may be loaded")
        return URLFetcherResponse(
            url,
            body=base64.b64decode(match[2], validate=True),
            headers={"Content-Type": match[1]},
        )

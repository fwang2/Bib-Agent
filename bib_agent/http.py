from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request


class HttpClient:
    def __init__(self, min_interval_seconds: float = 1.0, timeout_seconds: int = 20):
        self.min_interval_seconds = min_interval_seconds
        self.timeout_seconds = timeout_seconds
        self._last_request_at = 0.0

    def _throttle(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_request_at
        if elapsed < self.min_interval_seconds:
            time.sleep(self.min_interval_seconds - elapsed)
        self._last_request_at = time.monotonic()

    def get_text(self, url: str, headers: dict | None = None) -> str:
        self._throttle()
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                "Accept-Language": "en-US,en;q=0.9",
                **(headers or {}),
            },
        )
        with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
            payload = response.read()
            charset = response.headers.get_content_charset() or "utf-8"
            return payload.decode(charset, errors="replace")

    def get_json(self, url: str, headers: dict | None = None) -> dict:
        text = self.get_text(url, headers=headers)
        return json.loads(text)


def safe_get_text(client: HttpClient, url: str, headers: dict | None = None) -> str | None:
    try:
        return client.get_text(url, headers=headers)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError):
        return None


def url_with_query(base_url: str, **params: str | int) -> str:
    return f"{base_url}?{urllib.parse.urlencode(params)}"

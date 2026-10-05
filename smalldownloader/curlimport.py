"""Turn a browser 'Copy as cURL' snippet into request parameters.

Only the parts a downloader cares about are extracted: the URL, the headers,
the HTTP method and the POST body. Line continuations from cmd.exe (``^``),
bash (``\\``) and PowerShell (`` ``` ``) are all stripped first, so a snippet
copied on any platform can be pasted into either front-end.
"""

from __future__ import annotations

import re
from typing import Dict, Optional

__all__ = ["parse_curl", "CurlRequest"]


class CurlRequest:
    """The pieces of a cURL command that matter for a download."""

    def __init__(
        self,
        url: Optional[str] = None,
        headers: Optional[Dict[str, str]] = None,
        method: Optional[str] = None,
        body: Optional[str] = None,
    ) -> None:
        self.url = url
        self.headers = headers if headers is not None else {}
        self.method = method
        self.body = body

    @property
    def is_post(self) -> bool:
        return (self.method or "GET").upper() not in ("GET", "HEAD") or self.body is not None

    def as_post_data(self) -> Optional[bytes]:
        return self.body.encode("utf-8") if self.body is not None else None

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "CurlRequest(url=%r, headers=%d, method=%r, body=%s)" % (
            self.url, len(self.headers), self.method, "yes" if self.body else "no"
        )


def parse_curl(raw: str) -> CurlRequest:
    """Parse a pasted cURL command. Missing pieces simply come back as ``None``."""
    cleaned = re.sub(r"[\^\\`]\r?\n\s*", " ", raw or "")   # line continuations
    # cmd.exe escapes the quoting itself: -H ^"cookie: x^"  ->  -H "cookie: x"
    cleaned = re.sub(r"\^(?=[\"'])", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()

    url: Optional[str] = None
    match = re.search(r"curl(?:\.exe)?\s+(?:-[^\s]+\s+)*?['\"]?(https?://[^\s'\"]+)", cleaned,
                      re.IGNORECASE)
    if match:
        url = match.group(1).rstrip("^").strip("'\"")
    if not url:
        match = re.search(r"['\"]?(https?://[^\s'\"]+)", cleaned)
        if match:
            url = match.group(1).rstrip("^").strip("'\"")

    headers: Dict[str, str] = {}
    for header_match in re.finditer(r"(?:-H|--header)\s+(['\"])(.*?)\1", cleaned):
        line = header_match.group(2)
        if ":" in line:
            key, value = line.split(":", 1)
            if key.strip().lower() != "accept-encoding":
                headers[key.strip()] = value.strip()

    body: Optional[str] = None
    for data_match in re.finditer(r"(?:--data-raw|--data-binary|--data|-d)\s+(['\"])(.*?)\1",
                                  cleaned):
        body = data_match.group(2)
        headers.setdefault("Content-Type", "application/json;charset=UTF-8")
        break

    method: Optional[str] = None
    explicit = re.search(r"-X\s+([A-Za-z]+)", cleaned)
    if explicit:
        method = explicit.group(1).upper()
    elif body is not None:
        method = "POST"

    return CurlRequest(url=url, headers=headers, method=method, body=body)

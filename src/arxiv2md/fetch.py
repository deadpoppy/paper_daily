"""Fetch and cache arXiv HTML pages."""

from __future__ import annotations

import asyncio
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import httpx

from arxiv2md.cache import evict_if_needed
from arxiv2md.config import (
    ARXIV2MD_CACHE_PATH,
    ARXIV2MD_CACHE_TTL_SECONDS,
    ARXIV2MD_FETCH_BACKOFF_S,
    ARXIV2MD_FETCH_MAX_RETRIES,
    ARXIV2MD_FETCH_TIMEOUT_S,
    ARXIV2MD_USER_AGENT,
)

_RETRY_STATUS = {429, 500, 502, 503, 504}


def _atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    """Write text to *path* atomically.

    Writes to a sibling temp file in the same directory and renames it into
    place via os.replace(). Guarantees the destination is never left in a
    partial state, even if the process is killed mid-write (Ctrl+C / OOM).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(text)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        raise


async def fetch_arxiv_html(
    html_url: str,
    *,
    arxiv_id: str,
    version: str | None,
    use_cache: bool = True,
    ar5iv_url: str | None = None,
) -> tuple[str, str]:
    """Fetch arXiv HTML and cache it locally.

    Tries html_url first (arxiv.org), then falls back to ar5iv_url if 404.

    Returns:
        A tuple of (html_text, source_url) where source_url is the URL that
        was actually used to fetch the HTML.
    """
    cache_dir = _cache_dir_for(arxiv_id, version)
    html_path = cache_dir / "source.html"
    source_url_path = cache_dir / "source_url.txt"

    if use_cache and _is_cache_fresh(html_path):
        cached_source_url = (
            source_url_path.read_text(encoding="utf-8").strip() if source_url_path.exists() else html_url
        )
        return html_path.read_text(encoding="utf-8"), cached_source_url

    # Try primary URL (arxiv.org) first.
    # No need to manually unlink stale cache: _atomic_write_text uses
    # os.replace() which atomically overwrites any existing file.
    try:
        html_text, final_url = await _fetch_with_retries(html_url)
        evict_if_needed()
        _atomic_write_text(html_path, html_text)
        _atomic_write_text(source_url_path, final_url)
        return html_text, final_url
    except RuntimeError as primary_error:
        # If we got 404 and have ar5iv fallback, try it
        if ar5iv_url and "does not have an HTML version" in str(primary_error):
            try:
                html_text, final_url = await _fetch_with_retries(ar5iv_url)
                evict_if_needed()
                _atomic_write_text(html_path, html_text)
                _atomic_write_text(source_url_path, final_url)
                return html_text, final_url
            except Exception:
                # If ar5iv also fails, raise the original error
                pass
        # Re-raise the original error
        raise primary_error


async def _fetch_with_retries(url: str) -> tuple[str, str]:
    """Fetch URL with retries. Returns (html_text, final_url) where final_url is the
    URL after following any redirects."""
    timeout = httpx.Timeout(ARXIV2MD_FETCH_TIMEOUT_S)
    headers = {"User-Agent": ARXIV2MD_USER_AGENT}
    last_exc: Exception | None = None

    for attempt in range(ARXIV2MD_FETCH_MAX_RETRIES + 1):
        try:
            async with httpx.AsyncClient(timeout=timeout, headers=headers, follow_redirects=True) as client:
                response = await client.get(url)

            # Check for 404 specifically to provide a better error message
            if response.status_code == 404:
                raise RuntimeError(
                    "This paper does not have an HTML version available on arXiv. "
                    "arxiv2md requires papers to be available in HTML format. "
                    "Older papers may only be available as PDF."
                )

            if response.status_code in _RETRY_STATUS:
                last_exc = RuntimeError(f"HTTP {response.status_code} from arXiv")
            else:
                response.raise_for_status()
                _ensure_html_response(response)
                return response.text, str(response.url)
        except (httpx.RequestError, httpx.HTTPStatusError, RuntimeError) as exc:
            last_exc = exc

        if attempt < ARXIV2MD_FETCH_MAX_RETRIES:
            backoff = ARXIV2MD_FETCH_BACKOFF_S * (2**attempt)
            await asyncio.sleep(backoff)

    raise RuntimeError(f"Failed to fetch HTML from {url}: {last_exc}")


def _ensure_html_response(response: httpx.Response) -> None:
    content_type = response.headers.get("content-type", "")
    if "text/html" not in content_type:
        raise ValueError(f"Unexpected content-type: {content_type}")


def _is_cache_fresh(html_path: Path) -> bool:
    if not html_path.exists():
        return False
    if ARXIV2MD_CACHE_TTL_SECONDS <= 0:
        return True
    mtime = datetime.fromtimestamp(html_path.stat().st_mtime, tz=timezone.utc)
    age_seconds = (datetime.now(timezone.utc) - mtime).total_seconds()
    return age_seconds <= ARXIV2MD_CACHE_TTL_SECONDS


def _cache_dir_for(arxiv_id: str, version: str | None) -> Path:
    base = arxiv_id
    if version and arxiv_id.endswith(version):
        base = arxiv_id[: -len(version)]
    version_tag = version or "latest"
    key = f"{base}__{version_tag}".replace("/", "_")
    return ARXIV2MD_CACHE_PATH / key

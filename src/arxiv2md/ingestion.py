"""Ingestion pipeline for arXiv HTML -> Markdown."""

from __future__ import annotations

import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from arxiv2md.fetch import fetch_arxiv_html
from arxiv2md.html_parser import parse_arxiv_html
from arxiv2md.markdown import convert_fragment_to_markdown
from arxiv2md.output_formatter import format_paper
from arxiv2md.schemas import IngestionResult
from arxiv2md.sections import filter_sections

_REFERENCE_TITLES = ("references", "bibliography")
_ABSTRACT_TITLE = "abstract"


def _truncate_references_html(html: str) -> str:
    """Remove bibliography/references sections from raw HTML before parsing.

    This is a *pre-processing* step that runs before section tree parsing,
    ensuring that long reference lists never enter the context window.
    """
    soup = BeautifulSoup(html, "html.parser")

    # Strategy 1: remove sections with bibliography class
    for section in soup.find_all("section", class_=re.compile(r"ltx_bibliography")):
        section.decompose()

    # Strategy 2: remove by heading text (fallback for non-standard HTML)
    for heading in soup.find_all(["h1", "h2", "h3"]):
        text = heading.get_text(strip=True).lower()
        if text in _REFERENCE_TITLES:
            section = heading.find_parent("section")
            if section:
                section.decompose()
            else:
                # No enclosing section: remove heading and subsequent siblings
                # until the next heading of same or higher level
                level = int(heading.name[1])
                heading.decompose()
                # Note: after decompose, find_next_siblings may behave oddly;
                # re-query from a nearby anchor if needed. For arXiv HTML,
                # bibliography is almost always inside <section>.

    return str(soup)


async def ingest_paper(
    *,
    arxiv_id: str,
    version: str | None,
    html_url: str,
    ar5iv_url: str | None = None,
    remove_refs: bool,
    remove_toc: bool,
    remove_inline_citations: bool = False,
    section_filter_mode: str,
    sections: list[str],
    include_frontmatter: bool = False,
) -> tuple[IngestionResult, dict[str, str | list[str] | None]]:
    """Fetch, parse, and serialize an arXiv paper into Markdown.

    Parameters
    ----------
    remove_inline_citations : bool
        If True, completely remove inline citation links from the output.
        If False (default), citation URLs are stripped but text is kept.
    """
    html, source_url = await fetch_arxiv_html(html_url, arxiv_id=arxiv_id, version=version, use_cache=True, ar5iv_url=ar5iv_url)

    # Use <base href> from the HTML if present (arXiv sets this for correct relative URL resolution)
    image_base_url = _extract_base_href(html, source_url)

    # Pre-process: truncate references *before* parsing to keep context clean
    if remove_refs:
        html = _truncate_references_html(html)

    parsed = parse_arxiv_html(html)

    filtered_sections = filter_sections(parsed.sections, mode=section_filter_mode, selected=sections)
    if remove_refs:
        filtered_sections = filter_sections(filtered_sections, mode="exclude", selected=_REFERENCE_TITLES)

    # Check if abstract should be included based on section filter
    selected_lower = [s.lower() for s in sections]
    if section_filter_mode == "exclude":
        include_abstract = _ABSTRACT_TITLE not in selected_lower
    else:  # include mode
        include_abstract = not sections or _ABSTRACT_TITLE in selected_lower

    for section in filtered_sections:
        _populate_section_markdown(section, remove_inline_citations=remove_inline_citations, base_url=image_base_url)

    result = format_paper(
        arxiv_id=arxiv_id,
        version=version,
        title=parsed.title,
        authors=parsed.authors,
        authors_block=parsed.authors_block,
        abstract=parsed.abstract if include_abstract else None,
        sections=filtered_sections,
        include_toc=not remove_toc,
        include_abstract_in_tree=parsed.abstract is not None,
        include_frontmatter=include_frontmatter,
    )

    metadata = {
        "title": parsed.title,
        "authors": parsed.authors,
        "authors_block": parsed.authors_block,
        "abstract": parsed.abstract,
    }

    return result, metadata


def _populate_section_markdown(section, *, remove_inline_citations: bool = False, base_url: str | None = None) -> None:
    if section.html:
        section.markdown = convert_fragment_to_markdown(section.html, remove_inline_citations=remove_inline_citations, base_url=base_url)
    for child in section.children:
        _populate_section_markdown(child, remove_inline_citations=remove_inline_citations, base_url=base_url)


def _extract_base_href(html: str, fallback_url: str) -> str:
    """Extract the <base href> from HTML and resolve it against fallback_url.

    arXiv HTML pages include a <base href='/html/<id>vN/'> that browsers use
    to resolve relative image paths. We must honour it too.
    """
    match = re.search(r'<base[^>]+href=["\']([^"\']+)["\']', html, re.IGNORECASE)
    if match:
        return urljoin(fallback_url, match.group(1))
    return fallback_url

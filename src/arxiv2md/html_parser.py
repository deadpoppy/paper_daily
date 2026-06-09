"""Parse arXiv HTML into metadata and section structure."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from arxiv2md.schemas import SectionNode


try:
    from bs4 import BeautifulSoup
    from bs4.element import NavigableString, Tag
except ImportError as exc:  # pragma: no cover - runtime dependency check
    raise RuntimeError("BeautifulSoup4 is required for HTML parsing (pip install beautifulsoup4).") from exc


_HEADING_RE = re.compile(r"^h[1-6]$")
_EMAIL_RE = re.compile(r"^[\w.+-]+@[\w.-]+\.\w+$")
_EMAIL_SEARCH_RE = re.compile(r"[\w.+-]+@[\w.-]+\.\w+")
# Keywords that indicate footnotes or contribution statements (case-insensitive check)
_SKIP_KEYWORDS = {"footnotemark:", "equal contribution", "work performed", "listing order"}
_MAX_AUTHOR_PART_LENGTH = 80  # Filter out long contribution statements
_CONTACT_KEYWORDS = {
    "university", "institute", "college", "department", "school",
    "laboratory", "lab", "center", "centre", "inc.", "ltd.", "corp.",
    "email", "e-mail", "mailto", "orcid", "address", "affiliation",
}


@dataclass
class ParsedArxivHtml:
    """Parsed content extracted from arXiv HTML."""

    title: str | None
    authors: list[str]
    authors_block: str | None
    abstract: str | None
    sections: list[SectionNode]


def parse_arxiv_html(html: str) -> ParsedArxivHtml:
    """Extract title, authors, abstract, and section tree from HTML."""
    soup = BeautifulSoup(html, "html.parser")
    document_root = _find_document_root(soup)

    title = _extract_title(soup)
    authors = _extract_authors(soup)
    authors_block = _extract_authors_block(soup)
    abstract = _extract_abstract(soup)
    sections = _extract_sections(document_root)

    return ParsedArxivHtml(
        title=title, authors=authors, authors_block=authors_block,
        abstract=abstract, sections=sections,
    )


def _find_document_root(soup: BeautifulSoup) -> Tag:
    root = soup.find("article", class_=re.compile(r"ltx_document"))
    if root:
        return root
    article = soup.find("article")
    if article:
        return article
    if soup.body:
        return soup.body
    return soup


def _extract_title(soup: BeautifulSoup) -> str | None:
    title_tag = soup.find("h1", class_=re.compile(r"ltx_title"))
    if title_tag:
        return title_tag.get_text(" ", strip=True)
    if soup.title:
        return soup.title.get_text(" ", strip=True)
    return None


def _extract_authors(soup: BeautifulSoup) -> list[str]:
    authors_container = soup.find("div", class_="ltx_authors")
    if not authors_container:
        document_root = _find_document_root(soup)
        authors_container = document_root.find("div", class_="ltx_authors")
    if not authors_container:
        return []

    author_nodes = authors_container.find_all(
        lambda tag: tag.name == "span"
        and "ltx_text" in tag.get("class", [])
        and "ltx_font_bold" in tag.get("class", [])
    )
    if not author_nodes:
        author_nodes = authors_container.find_all(
            class_=re.compile(r"\bltx_author\b|\bltx_personname\b")
        )

    authors: list[str] = []
    for node in author_nodes:
        for text in _clean_author_text(node):
            if text and text not in authors:
                authors.append(text)
    return authors


def _clean_author_text(node: Tag) -> list[str]:
    """Extract clean author names/affiliations, filtering out emails and footnotes."""
    clone = BeautifulSoup(str(node), "html.parser")
    # Remove superscripts (footnote markers)
    for sup in clone.find_all("sup"):
        sup.decompose()
    # Remove note elements
    for note in clone.find_all(class_=re.compile(r"ltx_note|ltx_role_footnote")):
        note.decompose()

    text = clone.get_text("\n", strip=True)
    parts = [re.sub(r"\s+", " ", part).strip() for part in text.splitlines()]

    cleaned: list[str] = []
    for part in parts:
        if not part:
            continue
        # Strip leading & (author separator in some papers)
        part = part.lstrip("&").strip()
        if not part:
            continue
        # Skip emails
        if _EMAIL_RE.match(part):
            continue
        # Skip pure numbers (footnote references)
        if part.isdigit():
            continue
        # Skip footnote markers and contribution keywords
        part_lower = part.lower()
        if any(marker in part_lower for marker in _SKIP_KEYWORDS):
            continue
        # Skip very long text (likely contribution statements)
        if len(part) > _MAX_AUTHOR_PART_LENGTH:
            continue
        # Skip text that looks like a sentence (contains multiple periods or common sentence patterns)
        if part.count(".") > 1 or (part.endswith(".") and len(part) > 40):
            continue
        cleaned.append(part)
    return cleaned


def _is_contact_info(text: str) -> bool:
    """Return True if *text* looks like an affiliation or email block."""
    text_lower = text.lower()
    if _EMAIL_SEARCH_RE.search(text):
        return True
    if any(kw in text_lower for kw in _CONTACT_KEYWORDS):
        return True
    return False


def _is_contribution_statement(text: str) -> bool:
    """Return True if *text* looks like an author-contribution footnote."""
    text_lower = text.lower()
    if any(marker in text_lower for marker in _SKIP_KEYWORDS):
        return True
    if len(text) > 120 and text.count(".") > 2:
        return True
    return False


def _extract_authors_block(soup: BeautifulSoup) -> str | None:
    """Extract the raw text block between title and abstract (authors, affiliations, emails).

    This preserves all metadata that appears in the ``ltx_authors`` container
    without trying to distinguish authors from institutions.
    """
    authors_container = soup.find("div", class_="ltx_authors")
    if not authors_container:
        document_root = _find_document_root(soup)
        authors_container = document_root.find("div", class_="ltx_authors")
    if not authors_container:
        return None

    # Work on a clone so we don't mutate the original soup.
    clone = BeautifulSoup(str(authors_container), "html.parser")

    # Remove all <sup> tags (footnote markers like 1, *, †).
    for sup in clone.find_all("sup"):
        sup.decompose()

    # Remove note / tag markers that are pure visual artifacts.
    for tag in list(clone.find_all(class_=re.compile(r"ltx_tag|ltx_note_mark"))):
        tag.decompose()

    # Remove empty visual-separator spans.
    for span in list(clone.find_all("span", class_="ltx_author_before")):
        if not span.get_text(strip=True):
            span.decompose()

    # Process ``ltx_note`` / ``ltx_role_footnote``.
    # In some papers these contain only a superscript mark (safe to drop);
    # in others they wrap the full affiliation / email text (must keep).
    for note in list(clone.find_all(class_=re.compile(r"ltx_note|ltx_role_footnote"))):
        content = note.find(class_=re.compile(r"ltx_note_content"))
        if content:
            text = content.get_text(" ", strip=True)
            text = re.sub(r"\s+", " ", text).strip()
            if _is_contact_info(text):
                note.replace_with(text)
                continue
            if _is_contribution_statement(text):
                note.decompose()
                continue
        # No content or short -> treat as a footnote mark and remove.
        note.decompose()

    # Process ``ltx_author_notes``.
    # These are also dual-purpose: they may hold ``ltx_contact`` children
    # (address / email) or long contribution paragraphs.
    for author_notes in list(clone.find_all(class_="ltx_author_notes")):
        has_contact = False
        for child in list(author_notes.children):
            if isinstance(child, Tag):
                classes = " ".join(child.get("class", []))
                if "ltx_contact" in classes:
                    has_contact = True
                else:
                    child.decompose()
            else:
                # Remove stray text / NavigableString nodes
                child.extract()
        if has_contact:
            continue
        # No explicit contact children: decide from plain text.
        text = author_notes.get_text(" ", strip=True)
        text = re.sub(r"\s+", " ", text).strip()
        if _is_contact_info(text):
            continue
        if _is_contribution_statement(text) or len(text) < 5:
            author_notes.decompose()

    text = clone.get_text(" ", strip=True)
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def _extract_abstract(soup: BeautifulSoup) -> str | None:
    abstract = soup.find(class_=re.compile(r"ltx_abstract"))
    if not abstract:
        return None
    return abstract.get_text(" ", strip=True)


def _extract_sections(root: Tag) -> list[SectionNode]:
    headings = [heading for heading in _iter_headings(root) if not _is_title_heading(heading)]
    sections: list[SectionNode] = []
    stack: list[SectionNode] = []

    for heading in headings:
        level = int(heading.name[1])
        title = heading.get_text(" ", strip=True)
        anchor = heading.get("id") or heading.parent.get("id")
        html = _collect_section_html(heading)

        node = SectionNode(title=title, level=level, anchor=anchor, html=html)

        while stack and stack[-1].level >= level:
            stack.pop()

        if stack:
            stack[-1].children.append(node)
        else:
            sections.append(node)

        stack.append(node)

    return sections


def _iter_headings(root: Tag) -> Iterable[Tag]:
    for heading in root.find_all(_HEADING_RE):
        if heading.find_parent("nav"):
            continue
        if heading.find_parent(class_=re.compile(r"ltx_abstract")):
            continue
        yield heading


def _is_title_heading(heading: Tag) -> bool:
    classes = heading.get("class", [])
    return "ltx_title_document" in classes


def _collect_section_html(heading: Tag) -> str | None:
    section = heading.find_parent("section")
    if not section:
        return None

    parts: list[str] = []
    started = False
    for child in section.children:
        if child == heading:
            started = True
            continue
        if not started:
            continue
        if isinstance(child, Tag) and child.name == "section":
            continue
        if isinstance(child, Tag) and any(
            cls.startswith("ltx_section") or cls.startswith("ltx_subsection") or cls.startswith("ltx_subsubsection")
            for cls in child.get("class", [])
        ):
            continue
        if isinstance(child, NavigableString):
            text = str(child)
            if text.strip():
                parts.append(text)
            continue
        if isinstance(child, Tag):
            parts.append(str(child))
    html = "".join(parts).strip()
    return html or None

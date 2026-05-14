"""Deduplicate and merge papers from multiple sources."""
from __future__ import annotations

import logging
from typing import Any

from rapidfuzz import fuzz

LOG = logging.getLogger("paper_daily.dedup")

TITLE_SIM_THRESHOLD = 0.88


def _norm_title(t: str | None) -> str:
    if not t:
        return ""
    return t.lower().strip().rstrip(".")


def _is_nonempty(val: Any) -> bool:
    if val is None:
        return False
    if isinstance(val, str) and not val.strip():
        return False
    if isinstance(val, list) and len(val) == 0:
        return False
    if isinstance(val, (int, float)) and val == 0:
        return False
    return True


# Field priority when merging (first = highest)
# arXiv is preferred as the primary data source.
FIELD_PRIORITY: dict[str, list[str]] = {
    "abstract": ["arxiv", "semantic_scholar", "openalex", "crossref", "pwc"],
    "tldr": ["semantic_scholar", "arxiv", "openalex", "crossref", "pwc"],
    "citation_count": ["semantic_scholar", "arxiv", "openalex", "crossref", "pwc"],
    "url": ["arxiv", "openalex", "semantic_scholar", "crossref", "pwc"],
    "venue": ["crossref", "arxiv", "openalex", "semantic_scholar", "pwc"],
    "authors": ["arxiv", "openalex", "semantic_scholar", "crossref", "pwc"],
}


def _merge_field(field: str, existing: dict, new_paper: dict) -> Any:
    existing_val = existing.get(field)
    new_val = new_paper.get(field)

    if field in ("authors",):
        # For lists, prefer longer / more detailed
        if isinstance(new_val, list) and isinstance(existing_val, list):
            return existing_val if len(existing_val) >= len(new_val) else new_val
        return existing_val or new_val

    priority = FIELD_PRIORITY.get(field)
    if not priority:
        return existing_val if _is_nonempty(existing_val) else new_val

    existing_source = existing.get("source", "").split(",")[0] if existing.get("source") else ""
    new_source = new_paper.get("source", "")

    def _rank(src: str) -> int:
        try:
            return priority.index(src)
        except ValueError:
            return 999

    if _is_nonempty(new_val) and _rank(new_source) < _rank(existing_source):
        return new_val
    return existing_val if _is_nonempty(existing_val) else new_val


def dedup_merge(papers: list[dict]) -> list[dict]:
    merged: list[dict] = []
    doi_index: dict[str, int] = {}
    arxiv_index: dict[str, int] = {}
    title_index: dict[str, int] = {}

    for p in papers:
        title = (p.get("title") or "").strip()
        if not title:
            LOG.debug("Skipping paper with empty title (doi=%s arxiv=%s)", p.get("doi"), p.get("arxiv_id"))
            continue
        doi = (p.get("doi") or "").strip().lower()
        arxiv_id = (p.get("arxiv_id") or "").strip().lower()
        norm = _norm_title(title)

        dup_idx = None
        if doi and doi in doi_index:
            dup_idx = doi_index[doi]
        elif arxiv_id and arxiv_id in arxiv_index:
            dup_idx = arxiv_index[arxiv_id]
        elif norm:
            for t, idx in title_index.items():
                if fuzz.ratio(norm, t) / 100.0 >= TITLE_SIM_THRESHOLD:
                    dup_idx = idx
                    break

        if dup_idx is not None:
            existing = merged[dup_idx]
            # Merge sources
            sources = set(existing.get("source", "").split(","))
            sources.add(p.get("source", ""))
            existing["source"] = ",".join(sorted(s for s in sources if s))
            # Merge identifiers
            for idf in ("doi", "arxiv_id"):
                if not existing.get(idf) and p.get(idf):
                    existing[idf] = p[idf]
            # Merge fields with priority
            for field in FIELD_PRIORITY:
                existing[field] = _merge_field(field, existing, p)
            # Keep best engine rank
            existing["_engine_rank"] = min(existing.get("_engine_rank", 999), p.get("_engine_rank", 999))
        else:
            idx = len(merged)
            merged.append(dict(p))
            if doi:
                doi_index[doi] = idx
            if arxiv_id:
                arxiv_index[arxiv_id] = idx
            if norm:
                title_index[norm] = idx

    LOG.info("Dedup: %d raw -> %d unique", len(papers), len(merged))
    return merged


def filter_seen(papers: list[dict], seen_arxivs: set[str]) -> list[dict]:
    """Filter out papers already recommended by arxiv_id."""
    filtered = []
    for p in papers:
        arxiv = (p.get("arxiv_id") or "").strip().lower()
        if arxiv and arxiv in seen_arxivs:
            continue
        filtered.append(p)
    LOG.info("History filter: %d -> %d new papers", len(papers), len(filtered))
    return filtered

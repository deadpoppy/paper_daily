"""Paper scoring and ranking engine.

Score = w_relevance * relevance + w_recency * recency + w_impact * impact + w_novelty * novelty
"""
from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from typing import Any

from paper_daily.semantic_ranker import get_semantic_ranker


def _tokenize(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]{2,}", text.lower()))


def _parse_date(date_str: str | None) -> datetime | None:
    if not date_str:
        return None
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        try:
            return datetime.strptime(date_str[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            return None


def score_relevance_lexical(paper: dict, topic_keywords: list[list[str]]) -> float:
    """Max Jaccard-like overlap with any topic keyword set (lexical fallback)."""
    text = " ".join([
        paper.get("title") or "",
        paper.get("abstract") or "",
        paper.get("tldr") or "",
    ])
    paper_tokens = _tokenize(text)
    if not paper_tokens:
        return 0.0

    best = 0.0
    for kws in topic_keywords:
        kw_tokens = _tokenize(" ".join(kws))
        if not kw_tokens:
            continue
        overlap = len(paper_tokens & kw_tokens)
        score = overlap / len(kw_tokens)
        best = max(best, min(score, 1.0))
    return best


def score_recency(paper: dict, now: datetime | None = None) -> float:
    if now is None:
        now = datetime.now(timezone.utc)

    pub = _parse_date(paper.get("published_date"))
    if not pub:
        return 0.5  # neutral

    days_old = max((now - pub).days, 0)
    if days_old <= 30:
        return 1.0
    if days_old <= 90:
        return 0.9
    if days_old <= 180:
        return 0.8
    return 0.8 * math.exp(-0.005 * (days_old - 180))


def score_impact(paper: dict) -> float:
    citations = paper.get("citation_count", 0) or 0
    cap = 200  # AI field rough cap
    raw = math.log(citations + 1)
    ceiling = math.log(cap + 1)
    return min(raw / ceiling, 1.0)


def score_novelty(paper: dict, seen_dois: set[str], seen_arxivs: set[str]) -> float:
    doi = (paper.get("doi") or "").strip().lower()
    arxiv = (paper.get("arxiv_id") or "").strip().lower()
    if doi and doi in seen_dois:
        return 0.0
    if arxiv and arxiv in seen_arxivs:
        return 0.0
    return 1.0


def rank_papers(
    papers: list[dict],
    topic_keywords: list[list[str]],
    seen_dois: set[str],
    seen_arxivs: set[str],
    w_relevance: float = 0.40,
    w_recency: float = 0.15,
    w_impact: float = 0.10,
    w_novelty: float = 0.10,
    w_academic_value: float = 0.25,
) -> list[dict]:
    now = datetime.now(timezone.utc)

    # --- Semantic relevance (batch, CPU, lightweight) ---
    ranker = get_semantic_ranker()
    semantic_scores = ranker.score_papers(papers, topic_keywords)
    if semantic_scores is not None:
        scoring_mode = "semantic"
    else:
        scoring_mode = "lexical"

    # --- Impact: steep relative decay within this batch ---
    # Highest citation in the batch gets 1.0; others decay as (relative)^2
    max_citations = max((p.get("citation_count", 0) or 0) for p in papers) if papers else 1
    max_citations = max(max_citations, 1)  # avoid div-by-zero

    scored = []
    for i, p in enumerate(papers):
        if semantic_scores is not None:
            rel = semantic_scores[i]
        else:
            rel = score_relevance_lexical(p, topic_keywords)
        rec = score_recency(p, now)
        nov = score_novelty(p, seen_dois, seen_arxivs)
        aca = p.get("academic_score", 0.5)  # 0-1, 0.5 = neutral if not yet assessed

        # Impact: relative citation with steep decay (square)
        citations = p.get("citation_count", 0) or 0
        relative = citations / max_citations
        imp = relative ** 1.8  # steep decay: 0.5 -> 0.25, 0.25 -> 0.0625

        total = (
            w_relevance * rel
            + w_recency * rec
            + w_impact * imp
            + w_novelty * nov
            + w_academic_value * aca
        )
        copy = dict(p)
        copy["_scores"] = {
            "relevance": round(rel, 4),
            "recency": round(rec, 4),
            "impact": round(imp, 4),
            "novelty": round(nov, 4),
            "academic_value": round(aca, 4),
            "total": round(total, 4),
            "scoring_mode": scoring_mode,
        }
        scored.append(copy)

    scored.sort(key=lambda x: x["_scores"]["total"], reverse=True)
    return scored

"""Multi-source async search with date filtering and cross-source dedup."""
from __future__ import annotations

import asyncio
import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

LOG = logging.getLogger("paper_daily.search")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _six_months_ago(days: int = 180) -> str:
    d = datetime.now(timezone.utc) - timedelta(days=days)
    return d.strftime("%Y-%m-%d")


def _parse_date(raw: str | None) -> str | None:
    if not raw:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m", "%Y"):
        try:
            return datetime.strptime(raw[: len(fmt)], fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _is_within_days(date_str: str | None, days: int) -> bool:
    if not date_str:
        return True  # keep if unknown
    try:
        pub = datetime.strptime(date_str[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        # Sanity check: reject obviously wrong future dates
        if pub.year > datetime.now(timezone.utc).year + 1:
            return False
        return pub >= cutoff
    except Exception:
        return True


def _sane_year(paper: dict) -> bool:
    """Reject papers with obviously bogus years (e.g. 2106, 2114 from bad CrossRef data)."""
    year = paper.get("year")
    if year is None:
        return True
    current_year = datetime.now(timezone.utc).year
    return 1990 <= year <= current_year + 1


def _build_arxiv_query(keywords: list[str], days_back: int) -> str:
    """Build an arXiv search query that uses OR across keywords.

    Example: (cat:cs.* OR cat:stat.*) AND (all:"large language model" OR all:LLM OR all:transformer)
    """
    since = datetime.now(timezone.utc) - timedelta(days=days_back)
    since_str = since.strftime("%Y%m%d")
    kw_parts = []
    for kw in keywords:
        if " " in kw:
            kw_parts.append(f'all:"{kw}"')
        else:
            kw_parts.append(f"all:{kw}")
    kw_query = " OR ".join(kw_parts)
    # Broaden categories beyond cs.* to catch stat.ML, physics.*, math.*, etc.
    cats = " OR ".join([
        "cat:cs.*", "cat:stat.*", "cat:physics.*", "cat:math.*",
        "cat:eess.*", "cat:q-bio.*", "cat:econ.*",
    ])
    return f"({cats}) AND ({kw_query}) AND submittedDate:[{since_str}0000 TO 999912312359]"


def _build_ss_query(keywords: list[str]) -> str:
    """Build a Semantic Scholar query using OR semantics.

    SS bulk search treats space-separated terms as AND. Use '|' for OR.
    Example: "large language model | LLM | transformer"
    """
    return " | ".join(keywords)


# ---------------------------------------------------------------------------
# 1. arXiv (Atom API via httpx)
# ---------------------------------------------------------------------------
_ARXIV_LOCK = asyncio.Lock()


async def _search_arxiv(keywords: list[str], max_results: int = 50, days_back: int = 180) -> list[dict]:
    q = _build_arxiv_query(keywords, days_back)
    url = "https://export.arxiv.org/api/query"
    ns = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
    opensearch_ns = "http://a9.com/-/spec/opensearch/1.1/"
    papers: list[dict] = []
    start = 0
    page_size = min(max_results, 1000)  # arXiv allows up to 2000; use 1000 to balance payload size and fewer requests

    async with _ARXIV_LOCK:
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            while len(papers) < max_results:
                params = {
                    "search_query": q,
                    "start": start,
                    "max_results": page_size,
                    "sortBy": "submittedDate",
                    "sortOrder": "descending",
                }
                attempt = 0
                while True:
                    try:
                        resp = await client.get(url, params=params)
                        if resp.status_code == 429:
                            # arXiv official: max 1 req/s (interval >= 3 s). Back off aggressively.
                            wait = max(3, min(2 ** attempt * 2, 120))
                            LOG.warning("arXiv rate limited (429), retry in %ds (attempt %d)", wait, attempt + 1)
                            await asyncio.sleep(wait)
                            attempt += 1
                            continue
                        resp.raise_for_status()
                        root = ET.fromstring(resp.text)

                        # Parse total results to know when to stop
                        total_el = root.find(f"{{{opensearch_ns}}}totalResults")
                        total_results = int(total_el.text) if total_el is not None else 0

                        entries = root.findall("atom:entry", ns)
                        if not entries:
                            break  # No more results

                        for entry in entries:
                            title = entry.findtext("atom:title", "", namespaces=ns).replace("\n", " ").strip()
                            summary = (entry.findtext("atom:summary", "", namespaces=ns) or "")[:600]
                            published = entry.findtext("atom:published", "", namespaces=ns)
                            arxiv_id = ""
                            id_el = entry.find("atom:id", ns)
                            if id_el is not None and id_el.text:
                                raw_id = id_el.text.split("/")[-1]
                                arxiv_id = raw_id.split("v")[0]
                            doi = ""
                            doi_el = entry.find("arxiv:doi", ns)
                            if doi_el is not None and doi_el.text:
                                doi = doi_el.text
                            authors = []
                            for author in entry.findall("atom:author", ns):
                                name = author.findtext("atom:name", "", namespaces=ns)
                                if name:
                                    authors.append(name)
                            pdf_url = ""
                            for link in entry.findall("atom:link", ns):
                                if link.get("title") == "pdf":
                                    pdf_url = link.get("href", "")
                                    break
                            year = None
                            pub_date = None
                            if published:
                                try:
                                    dt = datetime.strptime(published[:10], "%Y-%m-%d")
                                    year = dt.year
                                    pub_date = dt.strftime("%Y-%m-%d")
                                except ValueError:
                                    pass
                            papers.append({
                                "title": title,
                                "year": year,
                                "published_date": pub_date,
                                "arxiv_id": arxiv_id,
                                "doi": doi,
                                "citation_count": 0,
                                "authors": authors[:5],
                                "abstract": summary,
                                "venue": "arXiv",
                                "url": pdf_url or (f"https://arxiv.org/abs/{arxiv_id}" if arxiv_id else ""),
                                "source": "arxiv",
                            })
                        break  # Success, exit retry loop
                    except Exception as e:
                        # Respect official interval even on transient errors; never give up.
                        wait = max(3, min(2 ** attempt * 2, 120))
                        LOG.warning("arXiv error (attempt %d): %s, retry in %ds", attempt + 1, e, wait)
                        await asyncio.sleep(wait)
                        attempt += 1
                        # Never give up; loop again until success

                # Stop if we got fewer results than requested or reached total
                if len(entries) < page_size or start + len(entries) >= total_results:
                    break

                start += page_size
                # Official requirement: interval >= 3 s between requests
                await asyncio.sleep(3)

            # Ensure next caller also waits 3 s before its first request
            await asyncio.sleep(3)
    return papers[:max_results]


# ---------------------------------------------------------------------------
# 2. OpenAlex
# ---------------------------------------------------------------------------
async def _search_openalex(
    query: str, max_results: int = 50, days_back: int = 180, email: str | None = None
) -> list[dict]:
    since = _six_months_ago(days_back)
    papers = []
    cursor = "*"
    page_size = min(max_results, 200)  # OpenAlex max per page is 200
    pages_needed = (max_results + page_size - 1) // page_size

    async with httpx.AsyncClient(timeout=30) as client:
        for _ in range(pages_needed):
            if len(papers) >= max_results:
                break
            params = {
                "search": query,
                "per_page": page_size,
                "sort": "publication_date:desc",
                "filter": f"publication_date:>{since}",
                "cursor": cursor,
            }
            if email:
                params["mailto"] = email
            try:
                resp = await client.get("https://api.openalex.org/works", params=params)
                resp.raise_for_status()
                data = resp.json()
                for w in data.get("results", []):
                    try:
                        doi = (w.get("doi") or "").replace("https://doi.org/", "")
                        pub_date = w.get("publication_date") or str(w.get("publication_year") or "")
                        authors = []
                        for a in (w.get("authorships") or [])[:5]:
                            author_name = ""
                            if isinstance(a, dict):
                                author_info = a.get("author")
                                if isinstance(author_info, dict):
                                    author_name = author_info.get("display_name", "")
                            if author_name:
                                authors.append(author_name)
                        primary_loc = w.get("primary_location") or {}
                        source_name = ""
                        if isinstance(primary_loc, dict):
                            src = primary_loc.get("source") or {}
                            if isinstance(src, dict):
                                source_name = src.get("display_name", "")
                        best_oa = w.get("best_oa_location") or {}
                        pdf_url = ""
                        if isinstance(best_oa, dict):
                            pdf_url = best_oa.get("pdf_url", "")
                        papers.append({
                            "title": (w.get("display_name") or ""),
                            "year": w.get("publication_year"),
                            "published_date": _parse_date(pub_date),
                            "doi": doi,
                            "citation_count": w.get("cited_by_count", 0) or 0,
                            "authors": authors,
                            "abstract": (w.get("abstract") or "")[:600],
                            "venue": source_name,
                            "url": pdf_url or (f"https://doi.org/{doi}" if doi else ""),
                            "source": "openalex",
                        })
                    except Exception as inner:
                        LOG.debug("OpenAlex record parse error: %s", inner)
                        continue
                cursor = data.get("meta", {}).get("next_cursor")
                if not cursor:
                    break
            except Exception as e:
                LOG.warning("OpenAlex error: %s", e)
                break
    return papers[:max_results]


# ---------------------------------------------------------------------------
# 3. Semantic Scholar (bulk endpoint for better throughput & date filtering)
# ---------------------------------------------------------------------------
_SS_LOCK = asyncio.Lock()


async def _search_semantic_scholar(
    query: str, max_results: int = 50, days_back: int = 180
) -> list[dict]:
    since = (datetime.now(timezone.utc) - timedelta(days=days_back)).strftime("%Y-%m-%d")
    fields = "title,abstract,citationCount,year,authors,externalIds,openAccessPdf,venue,publicationDate"
    papers = []
    token = None

    async with _SS_LOCK:
        async with httpx.AsyncClient(timeout=60) as client:
            while len(papers) < max_results:
                params: dict[str, Any] = {
                    "query": query,
                    "fields": fields,
                    "publicationDateOrYear": f"{since}:",
                }
                if token:
                    params["token"] = token

                try:
                    resp = await client.get(
                        "https://api.semanticscholar.org/graph/v1/paper/search/bulk",
                        params=params,
                    )
                    if resp.status_code == 429:
                        wait = 5
                        LOG.warning("Semantic Scholar rate limited, waiting %ds...", wait)
                        await asyncio.sleep(wait)
                        continue
                    resp.raise_for_status()
                    data = resp.json()
                    batch = data.get("data", [])
                    for p in batch:
                        try:
                            ext = p.get("externalIds") or {}
                            pub_date = p.get("publicationDate")
                            papers.append({
                                "title": (p.get("title") or ""),
                                "year": p.get("year"),
                                "published_date": _parse_date(pub_date) if pub_date else None,
                                "doi": ext.get("DOI", ""),
                                "arxiv_id": ext.get("ArXiv", ""),
                                "citation_count": p.get("citationCount", 0) or 0,
                                "authors": [a.get("name", "") for a in (p.get("authors") or [])[:5]],
                                "abstract": (p.get("abstract") or "")[:600],
                                "venue": p.get("venue", ""),
                                "url": (p.get("openAccessPdf") or {}).get("url", "")
                                     or (f"https://arxiv.org/abs/{ext.get('ArXiv', '')}" if ext.get("ArXiv") else "")
                                     or (f"https://doi.org/{ext.get('DOI', '')}" if ext.get("DOI") else ""),
                                "source": "semantic_scholar",
                            })
                        except Exception as inner:
                            LOG.debug("SS record parse error: %s", inner)
                            continue

                    token = data.get("token")
                    if not token or not batch:
                        break
                except Exception as e:
                    LOG.warning("Semantic Scholar error: %s", e)
                    break

    return papers[:max_results]


# ---------------------------------------------------------------------------
# 4. CrossRef
# ---------------------------------------------------------------------------
async def _search_crossref(
    query: str, max_results: int = 50, days_back: int = 180
) -> list[dict]:
    since = _six_months_ago(days_back)
    papers = []
    async with httpx.AsyncClient(timeout=30) as client:
        params = {
            "query": query,
            "filter": f"from-pub-date:{since}",
            "rows": min(max_results, 200),
            "sort": "published",
            "order": "desc",
        }
        headers = {"User-Agent": "PaperDaily/0.1 (mailto:paperdaily@localhost)"}
        try:
            resp = await client.get(
                "https://api.crossref.org/works", params=params, headers=headers
            )
            resp.raise_for_status()
            items = resp.json().get("message", {}).get("items", [])
            for item in items:
                try:
                    pub_date_parts = item.get("published-print", {}).get("date-parts", [[]])[0] or \
                                     item.get("published-online", {}).get("date-parts", [[]])[0] or []
                    year = pub_date_parts[0] if len(pub_date_parts) > 0 else None
                    month = pub_date_parts[1] if len(pub_date_parts) > 1 else None
                    day = pub_date_parts[2] if len(pub_date_parts) > 2 else None
                    date_str = f"{year:04d}-{month:02d}-{day:02d}" if year and month and day else \
                               (f"{year:04d}-{month:02d}-01" if year and month else (str(year) if year else None))
                    authors = []
                    for au in item.get("author", [])[:5]:
                        given = au.get("given", "")
                        family = au.get("family", "")
                        authors.append(f"{given} {family}".strip())
                    raw_abstract = item.get("abstract", "") or ""
                    clean_abstract = re.sub(r"<[^>]+>", "", raw_abstract).strip()[:600]
                    papers.append({
                        "title": item.get("title", [""])[0],
                        "year": year,
                        "published_date": _parse_date(date_str) if date_str else None,
                        "doi": item.get("DOI", ""),
                        "citation_count": item.get("is-referenced-by-count", 0) or 0,
                        "authors": authors,
                        "abstract": clean_abstract,
                        "venue": item.get("container-title", [""])[0],
                        "url": item.get("URL", ""),
                        "source": "crossref",
                    })
                except Exception as inner:
                    LOG.debug("CrossRef record parse error: %s", inner)
                    continue
        except Exception as e:
            LOG.warning("CrossRef error: %s", e)
    return papers


# ---------------------------------------------------------------------------
# Cross-source dedup helper (prefer arXiv)
# ---------------------------------------------------------------------------
def _dedup_within_batch(papers: list[dict]) -> list[dict]:
    """Deduplicate papers within a single search batch, preferring arXiv sources."""
    seen: dict[str, dict] = {}  # key -> best paper

    def _key(p: dict) -> str | None:
        arxiv = (p.get("arxiv_id") or "").strip().lower()
        if arxiv:
            return f"arxiv:{arxiv}"
        doi = (p.get("doi") or "").strip().lower()
        if doi:
            return f"doi:{doi}"
        title = (p.get("title") or "").strip().lower()
        if title:
            return f"title:{title}"
        return None

    def _is_arxiv(p: dict) -> bool:
        return "arxiv" in (p.get("source") or "").lower()

    for p in papers:
        k = _key(p)
        if not k:
            continue
        existing = seen.get(k)
        if existing is None:
            seen[k] = dict(p)
        elif _is_arxiv(p) and not _is_arxiv(existing):
            # Prefer arXiv version
            seen[k] = dict(p)
        else:
            # Merge sources list
            existing_sources = set(existing.get("source", "").split(","))
            existing_sources.add(p.get("source", ""))
            existing["source"] = ",".join(sorted(s for s in existing_sources if s))
            # Merge identifiers
            for idf in ("doi", "arxiv_id"):
                if not existing.get(idf) and p.get(idf):
                    existing[idf] = p[idf]
            # Keep best citation count
            existing["citation_count"] = max(
                existing.get("citation_count", 0) or 0,
                p.get("citation_count", 0) or 0,
            )

    return list(seen.values())


# ---------------------------------------------------------------------------
# Unified search
# ---------------------------------------------------------------------------
async def search_all(
    query: str | None = None,
    keywords: list[str] | None = None,
    max_results: int = 50,
    days_back: int = 180,
    email: str | None = None,
) -> list[dict]:
    """Search across multiple academic sources.

    * keywords are used for arXiv (structured OR query).
    * query (free-text) is used for OpenAlex, Semantic Scholar, CrossRef.
    """
    if not keywords:
        keywords = [query] if query else []
    if not query:
        query = " ".join(keywords)

    # Semantic Scholar uses '|' for OR semantics; plain space = AND.
    ss_query = _build_ss_query(keywords)

    tasks = [
        _search_arxiv(keywords, max_results, days_back),
        _search_openalex(query, max_results, days_back, email),
        _search_semantic_scholar(ss_query, max_results, days_back),
        _search_crossref(query, max_results, days_back),
    ]
    names = ["arxiv", "openalex", "semantic_scholar", "crossref"]

    results = await asyncio.gather(*tasks, return_exceptions=True)
    all_papers: list[dict] = []
    for name, res in zip(names, results):
        if isinstance(res, Exception):
            LOG.warning("%s search failed: %s", name, res)
        elif res:
            LOG.info("%s: %d papers", name, len(res))
            for rank, p in enumerate(res):
                p["_engine_rank"] = rank
            all_papers.extend(res)
        else:
            LOG.info("%s: 0 papers", name)

    # Cross-source dedup (prefer arXiv)
    deduped = _dedup_within_batch(all_papers)
    LOG.info("After cross-source dedup: %d / %d papers", len(deduped), len(all_papers))

    # Final date filter (safety net) + sanity year filter
    filtered = [
        p for p in deduped
        if _is_within_days(p.get("published_date"), days_back) and _sane_year(p)
    ]
    # Sort by publication date descending so the most recent papers come first
    filtered.sort(
        key=lambda p: p.get("published_date") or "0000-00-00",
        reverse=True,
    )
    LOG.info("After date filter: %d / %d papers", len(filtered), len(deduped))
    return filtered

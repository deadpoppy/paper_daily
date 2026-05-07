"""Multi-source async search with date filtering and cross-source dedup."""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import arxiv
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
# 1. arXiv (official arxiv package)
# ---------------------------------------------------------------------------
_ARXIV_LOCK = asyncio.Lock()


def _normalise_arxiv_result(result: arxiv.Result) -> dict:
    """Convert an arxiv.Result to the unified paper schema."""
    arxiv_id = result.entry_id.split("/abs/")[-1] if result.entry_id else ""
    if arxiv_id and "v" in arxiv_id:
        arxiv_id_base = arxiv_id.rsplit("v", 1)
        if len(arxiv_id_base) == 2 and arxiv_id_base[1].isdigit():
            arxiv_id = arxiv_id_base[0]

    authors = [str(a) for a in (result.authors or [])]
    pdf_url = result.pdf_url or ""

    year = None
    pub_date = None
    if result.published:
        year = result.published.year
        pub_date = result.published.strftime("%Y-%m-%d")

    doi = result.doi or ""

    return {
        "title": (result.title or "").replace("\n", " ").strip(),
        "year": year,
        "published_date": pub_date,
        "arxiv_id": arxiv_id,
        "doi": doi,
        "citation_count": 0,
        "authors": authors[:5],
        "abstract": result.summary or "",
        "venue": "arXiv",
        "url": pdf_url or (f"https://arxiv.org/abs/{arxiv_id}" if arxiv_id else ""),
        "source": "arxiv",
    }


async def _search_arxiv(keywords: list[str], max_results: int = 50, days_back: int = 180) -> list[dict]:
    q = _build_arxiv_query(keywords, days_back)

    def _run() -> list[dict]:
        client = arxiv.Client(
            page_size=min(max_results, 1000),
            delay_seconds=3,
            num_retries=500,
        )
        search = arxiv.Search(
            query=q,
            max_results=max_results,
            sort_by=arxiv.SortCriterion.SubmittedDate,
            sort_order=arxiv.SortOrder.Descending,
        )
        return [_normalise_arxiv_result(r) for r in client.results(search)]

    async with _ARXIV_LOCK:
        try:
            papers = await asyncio.to_thread(_run)
        except Exception as e:
            LOG.warning("arXiv search failed: %s", e)
            papers = []
        # Buffer before releasing lock to stay well below arXiv rate limits
        await asyncio.sleep(1)
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
                            "abstract": (w.get("abstract") or ""),
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
                                "abstract": (p.get("abstract") or ""),
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
                    LOG.warning("Semantic Scholar error: %s, Retrying...", e)
                    # break

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
                    clean_abstract = re.sub(r"<[^>]+>", "", raw_abstract).strip()
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
# arXiv resolution helpers
# ---------------------------------------------------------------------------
async def _fetch_arxiv_by_ids(arxiv_ids: list[str]) -> dict[str, dict]:
    """Batch-fetch arXiv papers by their IDs using the official arxiv package.

    Returns a dict mapping lowercase arxiv_id -> paper dict.
    """
    if not arxiv_ids:
        return {}

    unique_ids = list(dict.fromkeys(arxiv_ids))
    result: dict[str, dict] = {}

    async with _ARXIV_LOCK:
        for i in range(0, len(unique_ids), 50):
            batch = unique_ids[i : i + 50]

            def _run() -> list[dict]:
                client = arxiv.Client(
                    page_size=len(batch),
                    delay_seconds=3,
                    num_retries=500,
                )
                search = arxiv.Search(id_list=batch, max_results=len(batch))
                return [_normalise_arxiv_result(r) for r in client.results(search)]

            try:
                papers = await asyncio.to_thread(_run)
            except Exception as e:
                LOG.warning("arXiv id_list batch failed: %s", e)
                papers = []

            for p in papers:
                if p.get("arxiv_id"):
                    result[p["arxiv_id"].lower()] = p

            if i + 50 < len(unique_ids):
                await asyncio.sleep(3)

        await asyncio.sleep(1)

    return result


async def _search_arxiv_by_title(title: str) -> dict | None:
    """Search arXiv for a paper by exact title. Returns paper dict if found, else None."""
    if not title or len(title) < 5:
        return None

    safe_title = title.replace('"', "").strip()
    q = f'ti:"{safe_title}"'

    def _run() -> list[arxiv.Result]:
        client = arxiv.Client(
            page_size=10,
            delay_seconds=3,
            num_retries=500,
        )
        search = arxiv.Search(query=q, max_results=10)
        return list(client.results(search))

    async with _ARXIV_LOCK:
        try:
            results = await asyncio.to_thread(_run)
        except Exception as e:
            LOG.warning("arXiv title search failed: %s", e)
            return None

    if not results:
        return None

    search_norm = re.sub(r"[^\w]", "", title.lower())
    best = None
    best_score = 0

    for result in results:
        entry_title = (result.title or "").replace("\n", " ").strip()
        entry_norm = re.sub(r"[^\w]", "", entry_title.lower())
        if entry_norm == search_norm:
            best = result
            break
        if search_norm in entry_norm or entry_norm in search_norm:
            score = len(set(search_norm) & set(entry_norm))
            if score > best_score:
                best_score = score
                best = result

    if best is None:
        return None

    return _normalise_arxiv_result(best)


async def _resolve_arxiv_versions(papers: list[dict]) -> list[dict]:
    """For non-arXiv papers, try to find their arXiv versions.

    Papers that already come from arXiv are kept as-is.
    Non-arXiv papers with an arxiv_id are batch-resolved via id_list.
    Non-arXiv papers without an arxiv_id are searched by title one-by-one.

    Returns only papers that have a valid arXiv entry (either original or resolved).
    """
    arxiv_papers: list[dict] = []
    non_arxiv: list[dict] = []

    for p in papers:
        if (p.get("source") or "").lower() == "arxiv":
            arxiv_papers.append(dict(p))
        else:
            non_arxiv.append(dict(p))

    if not non_arxiv:
        return arxiv_papers

    # Phase 1: batch-resolve papers that already have an arxiv_id
    with_ids = [p for p in non_arxiv if p.get("arxiv_id")]
    without_ids = [p for p in non_arxiv if not p.get("arxiv_id")]

    resolved_by_id: dict[str, dict] = {}
    if with_ids:
        id_list = [p["arxiv_id"] for p in with_ids]
        LOG.info("Resolving %d non-arXiv papers by arxiv_id...", len(with_ids))
        resolved_by_id = await _fetch_arxiv_by_ids(id_list)
        LOG.info("Resolved %d/%d by arxiv_id", len(resolved_by_id), len(with_ids))

    # Phase 2: title search for remaining papers
    resolved_by_title: list[dict] = []
    if without_ids:
        LOG.info("Searching arXiv by title for %d papers...", len(without_ids))
        for idx, p in enumerate(without_ids):
            arxiv_paper = await _search_arxiv_by_title(p.get("title", ""))
            if arxiv_paper:
                if not arxiv_paper.get("citation_count") and p.get("citation_count"):
                    arxiv_paper["citation_count"] = p["citation_count"]
                if not arxiv_paper.get("doi") and p.get("doi"):
                    arxiv_paper["doi"] = p["doi"]
                resolved_by_title.append(arxiv_paper)
                LOG.debug(
                    "Title search resolved: %s -> %s", p.get("title", "")[:60], arxiv_paper.get("arxiv_id")
                )
            else:
                LOG.debug("Title search missed: %s", p.get("title", "")[:60])
            if (idx + 1) % 5 == 0:
                LOG.info("Title search progress: %d/%d", idx + 1, len(without_ids))

    final: list[dict] = list(arxiv_papers)

    for p in with_ids:
        arxiv_id = (p.get("arxiv_id") or "").lower()
        if arxiv_id in resolved_by_id:
            arxiv_paper = dict(resolved_by_id[arxiv_id])
            if not arxiv_paper.get("citation_count") and p.get("citation_count"):
                arxiv_paper["citation_count"] = p["citation_count"]
            if not arxiv_paper.get("doi") and p.get("doi"):
                arxiv_paper["doi"] = p["doi"]
            final.append(arxiv_paper)

    final.extend(resolved_by_title)

    return final


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
    debug: bool = False,
    sources: list[str] | None = None,
    resolve_arxiv: bool = False,
) -> list[dict]:
    """Search across multiple academic sources.

    * keywords are used for arXiv (structured OR query).
    * query (free-text) is used for OpenAlex, Semantic Scholar, CrossRef.
    * sources controls which engines to query (default: arxiv, semantic_scholar).
    * resolve_arxiv triggers expensive title/id resolution via extra arXiv API calls.
    """
    if not keywords:
        keywords = [query] if query else []
    if not query:
        query = " ".join(keywords)

    sources = sources or ["arxiv", "semantic_scholar"]
    allowed = set(s.lower() for s in sources)

    # Semantic Scholar uses '|' for OR semantics; plain space = AND.
    ss_query = _build_ss_query(keywords)

    tasks: list[asyncio.Task] = []
    names: list[str] = []

    source_tasks = {
        "arxiv": lambda: _search_arxiv(keywords, max_results, days_back),
        "openalex": lambda: _search_openalex(query, max_results, days_back, email),
        "semantic_scholar": lambda: _search_semantic_scholar(ss_query, max_results, days_back),
        "crossref": lambda: _search_crossref(query, max_results, days_back),
    }

    for name, fn in source_tasks.items():
        if name in allowed:
            tasks.append(asyncio.create_task(fn()))
            names.append(name)

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

    if resolve_arxiv:
        # Expensive: extra arXiv API calls for non-arXiv papers
        arxiv_only = await _resolve_arxiv_versions(deduped)
    else:
        # Fast path: keep arXiv-source papers and any paper that already carries an arxiv_id
        arxiv_only = [
            p for p in deduped
            if (p.get("source") or "").lower() == "arxiv" or p.get("arxiv_id")
        ]
    LOG.info("After arXiv resolution: %d / %d papers", len(arxiv_only), len(deduped))

    # Final date filter (safety net) + sanity year filter
    filtered = [
        p for p in arxiv_only
        if _is_within_days(p.get("published_date"), days_back) and _sane_year(p)
    ]
    # Sort by publication date descending so the most recent papers come first
    filtered.sort(
        key=lambda p: p.get("published_date") or "0000-00-00",
        reverse=True,
    )
    LOG.info("After date filter: %d / %d papers", len(filtered), len(arxiv_only))

    if debug:
        print(f"\n[DEBUG] ====== search_all results ({len(filtered)} papers) ======")
        for i, p in enumerate(filtered, 1):
            print(
                f"  {i}. {p.get('title', 'N/A')} | "
                f"source={p.get('source')} | "
                f"arxiv_id={p.get('arxiv_id')} | "
                f"doi={p.get('doi')} | "
                f"published={p.get('published_date')}"
            )
        print("[DEBUG] ====== end search_all ======\n")

    return filtered


# ---------------------------------------------------------------------------
# arXiv HTML availability filter
# ---------------------------------------------------------------------------
async def filter_arxiv_html_available(
    papers: list[dict],
    concurrency: int = 20,
) -> list[dict]:
    """Filter papers to only those whose arXiv HTML page exists.

    Papers without an ``arxiv_id`` are kept as-is.
    For papers with an ``arxiv_id``, a HEAD request to
    ``https://arxiv.org/html/{arxiv_id}`` is made; only those returning
    HTTP 200 are kept.
    """
    if not papers:
        return []

    async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
        semaphore = asyncio.Semaphore(concurrency)

        async def _check(paper: dict) -> tuple[dict, bool]:
            arxiv_id = paper.get("arxiv_id")
            if not arxiv_id:
                return paper, True

            async with semaphore:
                url = f"https://arxiv.org/html/{arxiv_id}"
                try:
                    resp = await client.head(url)
                    return paper, resp.status_code == 200
                except Exception as exc:
                    LOG.warning("HEAD %s failed: %s", url, exc)
                    return paper, False

        results = await asyncio.gather(*[_check(p) for p in papers])

    kept = [p for p, ok in results if ok]
    removed = len(papers) - len(kept)
    if removed:
        LOG.info(
            "Filtered out %d/%d papers without arXiv HTML version",
            removed,
            len(papers),
        )
    return kept

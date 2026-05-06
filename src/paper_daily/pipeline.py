"""Main pipeline: search → dedup → rank → review → archive."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path

from paper_daily.academic_value import assess_papers
from paper_daily.config import Config
from paper_daily.database import PaperDatabase
from paper_daily.dedup import dedup_merge, filter_seen
from paper_daily.output import format_console, save_outputs
from paper_daily.ranker import rank_papers
from paper_daily.reviewer import generate_reasons
from paper_daily.search import search_all
from paper_daily.summarize import summarize_arxiv_papers

LOG = logging.getLogger("paper_daily.pipeline")


async def run_pipeline(cfg: Config) -> list[dict]:
    db_path = cfg.data_dir / "paper_daily.db"
    db = PaperDatabase(db_path)

    seen_dois = db.get_seen_dois()
    seen_arxivs = db.get_seen_arxiv_ids()
    seen_titles = db.get_seen_titles()

    # ------------------------------------------------------------------
    # 1. Search across topics
    # ------------------------------------------------------------------
    LOG.info("Starting search for %d topics", len(cfg.topics))
    all_raw: list[dict] = []

    # Build search queries from topic keywords
    async def _search_topic(topic):
        keywords = topic.keywords[:3]
        query = " ".join(keywords)
        LOG.info("Searching topic '%s': %s", topic.key, query)
        papers = await search_all(
            query=query,
            keywords=keywords,
            max_results=cfg.max_results_per_source,
            days_back=cfg.days_back,
            email=cfg.openalex_email,
            debug=cfg.debug,
        )
        # Tag with topic
        for p in papers:
            p.setdefault("topic_tags", [])
            if topic.key not in p["topic_tags"]:
                p["topic_tags"].append(topic.key)
        return papers

    # Limit concurrency to avoid rate limits
    semaphore = asyncio.Semaphore(2)

    async def _bounded_search(topic):
        async with semaphore:
            return await _search_topic(topic)

    tasks = [asyncio.create_task(_bounded_search(t)) for t in cfg.topics]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    for t, res in zip(cfg.topics, results):
        if isinstance(res, Exception):
            LOG.error("Topic '%s' search failed: %s", t.key, res)
        else:
            LOG.info("Topic '%s': %d papers", t.key, len(res))
            all_raw.extend(res)

    LOG.info("Total raw papers from all topics: %d", len(all_raw))
    if not all_raw:
        LOG.warning("No papers found. Try relaxing filters or check network.")
        return []

    if cfg.debug:
        print(f"\n[DEBUG] ====== Pipeline stage: search ======")
        print(f"Total raw papers from all topics: {len(all_raw)}")
        for t, res in zip(cfg.topics, results):
            if isinstance(res, list):
                print(f"  Topic '{t.key}': {len(res)} papers")
        print("[DEBUG] ===================================\n")

    # ------------------------------------------------------------------
    # 2. Dedup & merge
    # ------------------------------------------------------------------
    merged = dedup_merge(all_raw)
    if cfg.debug:
        print(f"\n[DEBUG] ====== Pipeline stage: dedup_merge ======")
        print(f"Before: {len(all_raw)} raw papers")
        print(f"After:  {len(merged)} unique papers")
        print("[DEBUG] ========================================\n")

    # ------------------------------------------------------------------
    # 3. Filter already recommended
    # ------------------------------------------------------------------
    new_papers = filter_seen(merged, seen_dois, seen_arxivs, seen_titles)
    if cfg.debug:
        print(f"\n[DEBUG] ====== Pipeline stage: filter_seen ======")
        print(f"Before history filter: {len(merged)} papers")
        print(f"After history filter:  {len(new_papers)} new papers")
        if not new_papers:
            print("No new papers found after history filtering.")
        print("[DEBUG] ========================================\n")
    if not new_papers:
        LOG.info("No new papers found after history filtering.")
        return []

    # ------------------------------------------------------------------
    # 4. Pre-ranking (4-dimension)
    # ------------------------------------------------------------------
    topic_keywords = [t.keywords for t in cfg.topics]
    pre_ranked = rank_papers(
        new_papers,
        topic_keywords=topic_keywords,
        seen_dois=seen_dois,
        seen_arxivs=seen_arxivs,
        w_relevance=cfg.w_relevance,
        w_recency=cfg.w_recency,
        w_impact=cfg.w_impact,
        w_novelty=cfg.w_novelty,
        w_academic_value=0.0,  # academic value not yet assessed
    )

    # Trim bottom N% of all pre-ranked papers, assess the rest
    trim_count = int(len(pre_ranked) * cfg.trim_ratio)
    candidates = pre_ranked[: len(pre_ranked) - trim_count] if trim_count else pre_ranked
    LOG.info(
        "Pre-ranked %d papers -> trimmed bottom %.0f%% (%d) -> %d candidates for academic assessment",
        len(pre_ranked), cfg.trim_ratio * 100, trim_count, len(candidates),
    )
    if cfg.debug:
        print(f"\n[DEBUG] ====== Pipeline stage: pre_rank & trim ======")
        print(f"Pre-ranked: {len(pre_ranked)} papers")
        print(f"Trim ratio: {cfg.trim_ratio*100:.0f}% -> removed {trim_count} papers")
        print(f"Candidates for academic assessment: {len(candidates)} papers")
        print("\nTop 5 after pre-rank:")
        for i, p in enumerate(pre_ranked[:5], 1):
            s = p.get("_scores", {})
            print(
                f"  {i}. {p.get('title', 'N/A')[:70]} | "
                f"total={s.get('total')} relevance={s.get('relevance')} "
                f"recency={s.get('recency')} impact={s.get('impact')} novelty={s.get('novelty')}"
            )
        print("[DEBUG] =============================================\n")

    # ------------------------------------------------------------------
    # 5. Academic-value assessment (LLM reviewer) — assess ALL candidates
    # ------------------------------------------------------------------
    candidates = await assess_papers(
        candidates,
        api_url=cfg.academic_value_url,
        api_key=cfg.academic_value_api_key,
        backup_api_key=cfg.academic_value_backup_api_key,
        db=db,
        model=cfg.academic_value_model,
        concurrency=10,
        debug=cfg.debug,
    )

    if cfg.debug:
        print(f"\n[DEBUG] ====== Pipeline stage: academic assessment done ======")
        print(f"Assessed candidates: {len(candidates)} papers")
        print("\nScores distribution:")
        for i, p in enumerate(candidates, 1):
            score = p.get("academic_score", 0.5)
            cached = "(cached)" if p.get("_academic_cached") else "(fresh)"
            print(f"  {i}. {p.get('title', 'N/A')[:60]} | academic_score={score:.2f} {cached}")
        print("[DEBUG] ====================================================\n")

    # ------------------------------------------------------------------
    # 6. Final ranking (5-dimension with academic_value)
    # ------------------------------------------------------------------
    final_ranked = rank_papers(
        candidates,
        topic_keywords=topic_keywords,
        seen_dois=seen_dois,
        seen_arxivs=seen_arxivs,
        w_relevance=cfg.w_relevance,
        w_recency=cfg.w_recency,
        w_impact=cfg.w_impact,
        w_novelty=cfg.w_novelty,
        w_academic_value=cfg.w_academic_value,
    )

    top_papers = final_ranked[: cfg.top_n]
    LOG.info("Final top %d papers selected", len(top_papers))

    if cfg.debug:
        print(f"\n[DEBUG] ====== Pipeline stage: final_rank ======")
        print(f"Final ranked: {len(final_ranked)} papers")
        print(f"Selected top: {len(top_papers)} papers")
        print("\nTop papers after final ranking:")
        for i, p in enumerate(top_papers, 1):
            s = p.get("_scores", {})
            print(
                f"  {i}. {p.get('title', 'N/A')[:70]} | "
                f"total={s.get('total')} relevance={s.get('relevance')} "
                f"recency={s.get('recency')} impact={s.get('impact')} "
                f"novelty={s.get('novelty')} academic_value={s.get('academic_value')}"
            )
        print("[DEBUG] =====================================\n")

    # ------------------------------------------------------------------
    # 7. Generate Chinese reasons (for the final top papers)
    # ------------------------------------------------------------------
    top_papers = await generate_reasons(
        top_papers,
        base_url=cfg.academic_value_url,
        api_key=cfg.academic_value_api_key,
        backup_api_key=cfg.academic_value_backup_api_key,
        model=cfg.academic_value_model,
        concurrency=6,
        debug=cfg.debug,
    )

    # ------------------------------------------------------------------
    # 8. Persist & output
    # ------------------------------------------------------------------
    save_outputs(top_papers, db, cfg.data_dir)

    # ------------------------------------------------------------------
    # 9. Summarize arXiv papers via hermes
    # ------------------------------------------------------------------
    summary_results = await summarize_arxiv_papers(top_papers)
    for sr in summary_results:
        if sr.get("success") and sr.get("has_content"):
            db.update_paper_summary_md_path(sr["arxiv_id"], sr["md_path"])

    # Console summary
    summary = format_console(top_papers)
    print(summary)
    return top_papers


def run(cfg: Config | None = None) -> list[dict]:
    if cfg is None:
        from paper_daily.config import load_config
        cfg = load_config()
    return asyncio.run(run_pipeline(cfg))

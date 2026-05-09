"""Academic value assessment via LLM (Anthropic-compatible API).

Uses a strict reviewer persona to score papers on true scholarly merit,
filtering out缝合/水文 and surfacing high-impact, highly-generalizable work.

Features:
- SQLite-based cache (key = SHA256 of title + abstract)
- Infinite retry with capped exponential backoff
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from typing import Any

import httpx

from paper_daily.database import PaperDatabase

LOG = logging.getLogger("paper_daily.academic_value")

_SYSTEM_PROMPT = (
'''
你是一位资深 NeurIPS/ICML/CVPR 审稿人，所在领域为具身智能、大模型、AI 算法与理论。
你的任务是根据论文标题和摘要，极其严格地评估其学术价值，给出 0–10 的分数。

【总体原则】
- 你所在顶会的接收率约 20%。这意味着 80% 的论文不应超过 5 分。
- 大多数投稿只是渐进式修改或已有方法的拼凑，你应当毫不犹豫地给低分。
- 如有任何犹豫（夸大字眼、方法似曾相识、无清晰 insight），一律从低打分。

【论文类型判定（先强制分类）】
在打分前，你必须先判断论文属于以下哪一类，并严格遵循对应的分数上限：
A. 综述性论文：上限 3 分，除非提出了全新的分析框架且极具指导意义，否则 ≤1 分。
B. 纯应用/工程/系统报告：上限 3 分，若无方法创新则 ≤1 分。
C. 数据集/基准/评测类：上限 2 分，若仅是简单收集或套用已有流程则 0 分。
D. 小众应用方向（无通用方法贡献，只是把已有技术用在某个窄域）：上限 2 分，若只是换场景调参则 0 分。
E. 理论研究/算法设计/新范式：使用下方严格评分标准。

【评分标准（仅适用于 E 类论文）】
- 10 分：里程碑。提出全新问题或范式，理论/算法有重大突破，将定义未来数年的研究方向。
- 8–9 分：重大创新。核心创意非常新颖，理论扎实，方法精巧，明显优于所有现有方法，拓展性强。
- 6–7 分：明显贡献。有明确的创新点，方法有理论或实验上的深入剖析，但尚未达到改变领域格局的程度。
- 4–5 分：边际改进。思路平庸，主要是对现有方法的简单改进或模块替换，实验没有令人信服的差距。
- 2–3 分：水文。缺乏实质性创新，A+B 拼接，实验不充分，或者仅在某些窄设定下有效。
- 0–1 分：完全不具备学术价值。严重错误、纯灌水、欺诈，或摘要空洞无物、纯宣传风格。

【关键惩罚规则（必须直接给 0 分，不适用其他分数）】
- 标题党、摘要使用“革命性”“超越人类”“首次”“通用人工智能”等夸大词汇但无实质内容 → 0 分。
- 只把 A、B、C 几个已有模块拼接，没有统一 insight 或设计原理说明 → 0 分。
- 摘要里没有一句话能说清楚“我们到底做了什么和别人不一样” → 2 分及以下。
- 仅汇报“我们在 XX 场景做了实验，效果不错”而无任何分析或启发 → 1 分。
'''+ "输出格式（必须严格遵守，不要输出任何多余内容）：\n"
    "<score>X.X</score>\n"
    "<reason>简短的中文理由，2–3句话说明为什么给这个分</reason>"
)


def _content_hash(paper: dict[str, Any]) -> str:
    """SHA256 of title + abstract for cache key (versioned)."""
    title = paper.get("title", "")
    abstract = paper.get("abstract") or paper.get("tldr") or ""
    raw = f"v{_CACHE_VERSION}::{title.strip()}::{abstract.strip()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _build_prompt(paper: dict[str, Any]) -> str:
    title = paper.get("title", "")
    abstract = (paper.get("abstract") or paper.get("tldr") or "").strip()
    venue = paper.get("venue") or paper.get("source", "")
    lines = [
        "请评估以下论文的学术价值。",
        "",
        f"标题：{title}",
    ]
    if venue:
        lines.append(f"来源：{venue}")
    if abstract:
        lines.append(f"摘要：{abstract}")
    lines.extend([
        "",
        "请给出学术价值评分（0–10）。",
    ])
    return "\n".join(lines)


# Bump this whenever the scoring logic changes so old cache entries are ignored.
_CACHE_VERSION = "2"


def _extract_score(text: str) -> float:
    """Extract score from <score>X.X</score> and normalize to [0,1].

    The system prompt explicitly asks the model for a 0–10 scale, so we
    **always** divide by 10.0.  This fixes the previous bug where a raw
    value of ``1.0`` (meaning 1/10, a low score) was treated as a perfect
    score because the code only divided when ``raw > 1.0``.
    """
    match = re.search(r"<score>\s*([\d.]+)\s*</score>", text)
    if not match:
        match = re.search(r"(?:score|分数)[：:]\s*([\d.]+)", text, re.I)
    if match:
        raw = float(match.group(1))
        # Prompt mandates 0-10 scale → always normalize.
        normalized = raw / 10.0
        return max(0.0, min(1.0, round(normalized, 4)))
    return 0.5


def _extract_reason(text: str) -> str:
    """Extract reason from <reason>...</reason>."""
    match = re.search(r"<reason>(.*?)</reason>", text, re.DOTALL)
    if match:
        return match.group(1).strip()
    paragraphs = [p.strip() for p in text.split("\n") if p.strip()]
    if paragraphs:
        return paragraphs[-1]
    return ""


async def _assess_one(
    client: httpx.AsyncClient,
    paper: dict[str, Any],
    api_url: str,
    api_key: str,
    backup_api_key: str | None,
    model: str,
    semaphore: asyncio.Semaphore,
    debug: bool = False,
) -> dict[str, Any]:
    prompt = _build_prompt(paper)
    payload = {
        "model": model,
        "max_tokens": 4096,
        "messages": [{"role": "user", "content": prompt}],
        "system": _SYSTEM_PROMPT,
    }

    keys = [k for k in (api_key, backup_api_key) if k]
    key_idx = 0
    attempts_per_key = 0
    total_attempt = 0

    while True:
        current_key = keys[key_idx]
        headers = {
            "x-api-key": current_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        if debug:
            print(f"\n[DEBUG] ====== Academic assessment request ======")
            print(f"Title: {paper.get('title', 'N/A')}")
            print(f"Payload:\n{payload}")
            print("[DEBUG] ==========================================\n")
        async with semaphore:
            try:
                resp = await client.post(
                    f"{api_url.rstrip('/')}/v1/messages",
                    headers=headers,
                    json=payload,
                )
                resp.raise_for_status()
                data = resp.json()

                content_blocks = data.get("content", [])
                text = ""
                if isinstance(content_blocks, list) and content_blocks:
                    for block in content_blocks:
                        if block.get("type") == "text":
                            text = block.get("text", "")
                            break
                    if not text:
                        text = content_blocks[0].get("text", "")
                elif isinstance(content_blocks, str):
                    text = content_blocks

                if debug:
                    print(f"\n[DEBUG] ====== Academic assessment response ======")
                    print(f"Title: {paper.get('title', 'N/A')}")
                    print(f"Raw text:\n{text}")
                    print(f"Extracted score: {text and _extract_score(text) or 'N/A'}")
                    print(f"Extracted reason: {text and _extract_reason(text) or 'N/A'}")
                    print("[DEBUG] ==========================================\n")

                if "<score>" not in text:
                    raise ValueError(f"Missing <score> tag in response: {text[:200]}")

                score = _extract_score(text)
                reason = _extract_reason(text)

                copy = dict(paper)
                copy["academic_score"] = score
                copy["academic_reason"] = reason
                LOG.debug("'%s' -> academic_score=%.2f", paper.get("title", "")[:40], score)
                return copy
            except Exception as e:
                wait = min(2 ** total_attempt, 30)  # cap at 30s
                LOG.warning(
                    "Academic assessment attempt %d failed for '%s' (key %d): %s. Retrying in %ds...",
                    total_attempt + 1,
                    paper.get("title", "")[:40],
                    key_idx + 1,
                    type(e).__name__,
                    wait,
                )
                await asyncio.sleep(wait)
                total_attempt += 1
                attempts_per_key += 1
                # Switch to backup key after 3 consecutive failures on current key
                if len(keys) > 1 and attempts_per_key >= 3:
                    key_idx = (key_idx + 1) % len(keys)
                    attempts_per_key = 0
                    LOG.info(
                        "Switching to API key %d for '%s' after 3 failures",
                        key_idx + 1,
                        paper.get("title", "")[:40],
                    )


async def assess_papers(
    papers: list[dict[str, Any]],
    api_url: str,
    api_key: str,
    db: PaperDatabase,
    backup_api_key: str | None = None,
    model: str = "MiniMax-M2.7",
    concurrency: int = 10,
    debug: bool = False,
) -> list[dict[str, Any]]:
    """Run academic-value assessment on a batch of papers (with cache)."""
    if not papers:
        return papers
    if not api_url or not api_key:
        LOG.info("No academic-value API configured, skipping assessment")
        for p in papers:
            p.setdefault("academic_score", 0.5)
            p.setdefault("academic_reason", "未配置学术评估 API")
        return papers

    # Split into cached vs uncached (try arxiv_id > doi > content_hash)
    to_assess: list[dict[str, Any]] = []
    cached_results: list[dict[str, Any]] = []
    for p in papers:
        cached = None
        arxiv_id = p.get("arxiv_id")
        if arxiv_id:
            cached = db.get_academic_cache_by_arxiv_id(arxiv_id)
        if not cached:
            doi = p.get("doi")
            if doi:
                cached = db.get_academic_cache_by_doi(doi)
        if not cached:
            h = _content_hash(p)
            cached = db.get_academic_cache(h)

        if cached:
            copy = dict(p)
            copy["academic_score"] = cached["academic_score"]
            copy["academic_reason"] = cached["academic_reason"]
            copy["_academic_cached"] = True
            cached_results.append(copy)
            if debug:
                print(
                    f"[DEBUG] Academic cache hit: '{p.get('title', 'N/A')[:60]}' -> "
                    f"score={cached['academic_score']:.2f}"
                )
            LOG.debug("Cache hit for '%s'", p.get("title", "")[:40])
        else:
            to_assess.append(p)

    LOG.info("Academic assessment: %d cached, %d need API call", len(cached_results), len(to_assess))
    if debug:
        print(
            f"\n[DEBUG] Academic assessment summary: {len(cached_results)} cached, "
            f"{len(to_assess)} need API call\n"
        )

    if to_assess:
        semaphore = asyncio.Semaphore(concurrency)
        limits = httpx.Limits(
            max_connections=max(50, concurrency * 5),
            max_keepalive_connections=max(20, concurrency * 3),
        )
        timeout = httpx.Timeout(180.0, connect=60.0, pool=90.0)
        async with httpx.AsyncClient(limits=limits, timeout=timeout) as client:
            tasks = [
                _assess_one(client, p, api_url, api_key, backup_api_key, model, semaphore, debug)
                for p in to_assess
            ]
            fresh_results = await asyncio.gather(*tasks)

        # Write cache for fresh results
        for p in fresh_results:
            db.set_academic_cache(
                _content_hash(p),
                p.get("arxiv_id"),
                p.get("doi"),
                p.get("title", ""),
                p.get("academic_score", 0.5),
                p.get("academic_reason", ""),
            )
        cached_results.extend(fresh_results)

    return cached_results

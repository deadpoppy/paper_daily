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
    "你是一位顶级 AI 会议的资深审稿人（如 NeurIPS、ICML、ICLR、CVPR、ACL）。\n"
    "你的任务是根据论文的标题和摘要，严格评估其学术价值，给出一个 0–10 的分数。\n\n"
    "评分标准（请严格把关，宁缺毋滥）：\n"
    "• 10 分：里程碑式工作，提出全新范式，具有深远影响力，多年后仍被广泛引用。\n"
    "• 8–9 分：核心创新清晰，方法有坚实理论支撑，实验设计严谨，拓展性强，对领域有显著推动。\n"
    "• 6–7 分：有明确贡献，方法或实验有一定新意，但属于增量式改进，拓展性一般。\n"
    "• 4–5 分：idea 平庸，主要是对现有方法的简单组合/拼接，实验勉强及格。\n"
    "• 2–3 分：水文特征明显，缺乏实质性创新，实验不充分或不可靠。\n"
    "• 0–1 分：学术欺诈、严重错误、纯粹灌水。\n\n"
    "请重点考察以下维度：\n"
    "1. 核心创新：是否提出了新的问题、新的方法或新的理论视角？\n"
    "2. 方法深度：是否有扎实的理论分析或精巧的算法设计？\n"
    "3. 实验质量：实验是否充分验证了核心 claim？\n"
    "4. 拓展性：这个工作是否能启发后续研究？是否容易被应用到其他场景？\n"
    "5. 区分度：与现有方法相比，优势是否显著且令人信服？\n\n"
    "注意事项：\n"
    "• 不要因为方向热门就给高分，也不要因为方向小众就给低分。\n"
    "• 对于标题党、摘要夸大但实际内容空洞的论文，请给出低分。\n"
    "• 对于拼接多个已有模块、缺乏统一 insight 的论文，请给出低分。\n\n"
    "输出格式（必须严格遵守，不要输出任何多余内容）：\n"
    "<score>X.X</score>\n"
    "<reason>简短的中文理由，2–3句话说明为什么给这个分</reason>"
)


def _content_hash(paper: dict[str, Any]) -> str:
    """SHA256 of title + abstract for cache key."""
    title = paper.get("title", "")
    abstract = paper.get("abstract") or paper.get("tldr") or ""
    raw = f"{title.strip()}::{abstract.strip()}"
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
        lines.append(f"摘要：{abstract[:900]}")
    lines.extend([
        "",
        "请给出学术价值评分（0–10）。",
    ])
    return "\n".join(lines)


def _extract_score(text: str) -> float:
    """Extract score from <score>X.X</score> and normalize to [0,1]."""
    match = re.search(r"<score>\s*([\d.]+)\s*</score>", text)
    if not match:
        match = re.search(r"(?:score|分数)[：:]\s*([\d.]+)", text, re.I)
    if match:
        raw = float(match.group(1))
        if raw > 1.0:
            raw = raw / 10.0
        return max(0.0, min(1.0, round(raw, 4)))
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
    model: str,
    semaphore: asyncio.Semaphore,
) -> dict[str, Any]:
    prompt = _build_prompt(paper)
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    payload = {
        "model": model,
        "max_tokens": 512,
        "messages": [{"role": "user", "content": prompt}],
        "system": _SYSTEM_PROMPT,
    }

    attempt = 0
    while True:
        async with semaphore:
            try:
                resp = await client.post(
                    f"{api_url.rstrip('/')}/v1/messages",
                    headers=headers,
                    json=payload,
                    timeout=120,
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
                wait = min(2 ** attempt, 30)  # cap at 30s
                LOG.warning(
                    "Academic assessment attempt %d failed for '%s': %s. Retrying in %ds...",
                    attempt + 1,
                    paper.get("title", "")[:40],
                    type(e).__name__,
                    wait,
                )
                await asyncio.sleep(wait)
                attempt += 1


async def assess_papers(
    papers: list[dict[str, Any]],
    api_url: str,
    api_key: str,
    db: PaperDatabase,
    model: str = "MiniMax-M2.7",
    concurrency: int = 3,
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

    # Split into cached vs uncached
    to_assess: list[dict[str, Any]] = []
    cached_results: list[dict[str, Any]] = []
    for p in papers:
        h = _content_hash(p)
        cached = db.get_academic_cache(h)
        if cached:
            copy = dict(p)
            copy["academic_score"] = cached["academic_score"]
            copy["academic_reason"] = cached["academic_reason"]
            copy["_academic_cached"] = True
            cached_results.append(copy)
            LOG.debug("Cache hit for '%s'", p.get("title", "")[:40])
        else:
            to_assess.append(p)

    LOG.info("Academic assessment: %d cached, %d need API call", len(cached_results), len(to_assess))

    if to_assess:
        semaphore = asyncio.Semaphore(concurrency)
        async with httpx.AsyncClient() as client:
            tasks = [
                _assess_one(client, p, api_url, api_key, model, semaphore)
                for p in to_assess
            ]
            fresh_results = await asyncio.gather(*tasks)

        # Write cache for fresh results
        for p in fresh_results:
            db.set_academic_cache(
                _content_hash(p),
                p.get("title", ""),
                p.get("academic_score", 0.5),
                p.get("academic_reason", ""),
            )
        cached_results.extend(fresh_results)

    return cached_results

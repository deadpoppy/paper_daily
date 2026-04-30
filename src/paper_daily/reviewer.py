"""Generate Chinese recommendation reasons via unified Anthropic-compatible LLM or fallback heuristics."""
from __future__ import annotations

import logging
from typing import Any

import httpx

LOG = logging.getLogger("paper_daily.reviewer")


def _build_prompt(paper: dict) -> str:
    title = paper.get("title", "")
    authors = ", ".join(paper.get("authors", [])[:3])
    abstract = (paper.get("abstract") or paper.get("tldr") or "")[:500]
    venue = paper.get("venue", "")
    sources = paper.get("source", "")

    return (
        "你是一位AI领域资深研究员。请为下面这篇论文写一段简洁的中文推荐理由，"
        "2-3句话，说明为什么AI研究者应该关注它。要求：\n"
        "1. 突出核心创新点或实际价值\n"
        "2. 语言精炼，避免泛泛而谈\n"
        "3. 不要只翻译标题，要体现你的专业判断\n\n"
        f"标题: {title}\n"
        f"作者: {authors}\n"
        f"来源: {venue or sources}\n"
        f"摘要: {abstract}\n\n"
        "推荐理由:"
    )


async def _call_anthropic_compatible(
    prompt: str,
    base_url: str,
    api_key: str,
    model: str,
) -> str:
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    payload = {
        "model": model,
        "max_tokens": 256,
        "messages": [{"role": "user", "content": prompt}],
    }
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            f"{base_url.rstrip('/')}/v1/messages",
            headers=headers,
            json=payload,
        )
        resp.raise_for_status()
        data = resp.json()

        content_blocks = data.get("content", [])
        if isinstance(content_blocks, list) and content_blocks:
            for block in content_blocks:
                if block.get("type") == "text":
                    return block.get("text", "").strip()
            return content_blocks[0].get("text", "").strip()
        return str(content_blocks).strip()


async def generate_reasons(
    papers: list[dict],
    base_url: str | None = None,
    api_key: str | None = None,
    model: str = "MiniMax-M2.7",
) -> list[dict]:
    """Attach Chinese recommendation reason to each paper."""
    if not papers:
        return papers

    # If no API configured, use fallback
    if not base_url or not api_key:
        LOG.info("No LLM API configured, using fallback reasons")
        return [_fallback_reason(p) for p in papers]

    results = []
    for p in papers:
        prompt = _build_prompt(p)
        try:
            reason = await _call_anthropic_compatible(prompt, base_url, api_key, model)
            # Clean up quotes
            reason = reason.strip('"').strip("'").strip()
            copy = dict(p)
            copy["reason_zh"] = reason
            results.append(copy)
            LOG.debug("Generated reason for '%s...'", p.get("title", "")[:40])
        except Exception as e:
            LOG.warning("LLM reason generation failed for '%s': %s", p.get("title", "")[:40], e)
            results.append(_fallback_reason(p))
    return results


def _fallback_reason(paper: dict) -> dict:
    """Simple heuristic reason when LLM is unavailable."""
    copy = dict(paper)
    title = paper.get("title", "")
    venue = paper.get("venue", "")
    sources = paper.get("source", "")
    tldr = paper.get("tldr", "")
    abstract = paper.get("abstract", "")

    # Simple keyword-based classification
    text = f"{title} {abstract}".lower()
    tags = []
    if any(k in text for k in ("llm", "language model", "transformer", "gpt")):
        tags.append("大语言模型")
    if any(k in text for k in ("vision", "image", "diffusion", "segmentation", "detection")):
        tags.append("计算机视觉")
    if any(k in text for k in ("reinforcement", "rlhf", "ppo", "q-learning")):
        tags.append("强化学习")
    if any(k in text for k in ("multimodal", "vision-language", "cross-modal")):
        tags.append("多模态")
    if any(k in text for k in ("agent", "autonomous", "tool use", "planning")):
        tags.append("智能体")
    if any(k in text for k in ("efficient", "quantization", "pruning", "distillation")):
        tags.append("模型效率")
    if any(k in text for k in ("interpretability", "explainable", "mechanistic")):
        tags.append("可解释性")
    if any(k in text for k in ("embodied", "robot", "manipulation", "humanoid", "locomotion")):
        tags.append("具身智能")
    if any(k in text for k in ("autonomous driving", "self-driving", "end-to-end driving")):
        tags.append("自动驾驶")
    if any(k in text for k in ("foundation model", "pre-training", "scaling law")):
        tags.append("基础模型")
    if any(k in text for k in ("world model", "environment model")):
        tags.append("世界模型")
    if any(k in text for k in ("slam", "localization", "mapping", "odometry")):
        tags.append("SLAM")
    if any(k in text for k in ("end-to-end learning", "end-to-end system")):
        tags.append("端到端")
    if any(k in text for k in ("neural network theory", "optimization landscape", "generalization", "representation learning")):
        tags.append("深度学习理论")
    if not tags:
        tags.append("AI算法")

    tag_str = " / ".join(tags)
    venue_str = venue or sources

    snippet = tldr or abstract
    if snippet:
        snippet = snippet[:80] + "..." if len(snippet) > 80 else snippet
        reason = f"【{tag_str}】该论文来自{venue_str}，关注{tag_str}方向的最新进展。{snippet}"
    else:
        reason = f"【{tag_str}】该论文来自{venue_str}，属于{tag_str}领域，值得AI研究者关注最新方法进展。"

    copy["reason_zh"] = reason
    return copy

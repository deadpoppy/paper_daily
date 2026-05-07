"""Summarize arXiv papers via CLI tool and save to ~/.paper_md/."""
from __future__ import annotations

import asyncio
import logging
import os
import re
from pathlib import Path

LOG = logging.getLogger("paper_daily.summarize")

_DEFAULT_SUMMARIZER = "claude"


def _get_summarizer() -> tuple[str, list[str]]:
    """Return (program, extra_args) based on PAPER_DAILY_SUMMARIZER env var."""
    backend = os.getenv("PAPER_DAILY_SUMMARIZER", _DEFAULT_SUMMARIZER).strip().lower()
    if backend == "hermes":
        return "hermes", ["chat", "-q"]
    if backend == "kimi-cli":
        return "kimi-cli", ["-p"]
    # default claude
    return "claude", ["-p"]


def _sanitize_filename(title: str) -> str:
    """Make a filesystem-safe filename from paper title."""
    # Remove/replace characters illegal on most filesystems
    safe = re.sub(r'[\\/:*?"<>|]', " ", title)
    # Collapse multiple spaces
    safe = re.sub(r"\s+", " ", safe).strip()
    # Limit length
    if len(safe) > 120:
        safe = safe[:120].strip()
    return safe + ".md"


def _check_md_content(md_path: Path) -> tuple[bool, int]:
    """Check if the MD file exists and has non-empty content."""
    if not md_path.exists():
        return False, 0
    try:
        content = md_path.read_text(encoding="utf-8")
        stripped = content.strip()
        return len(stripped) > 0, len(stripped)
    except Exception:
        return False, 0


async def summarize_arxiv_papers(
    papers: list[dict],
    output_dir: Path | None = None,
    concurrency: int = 1,
) -> list[dict]:
    """For each arXiv paper, invoke a CLI summarizer to produce an MD summary.

    Returns a list of dicts with keys: title, arxiv_id, md_path, success, has_content, content_length, error.
    """
    if output_dir is None:
        output_dir = Path.home() / ".paper_md"
    output_dir.mkdir(parents=True, exist_ok=True)

    # Filter to papers with an arXiv ID
    arxiv_papers = [p for p in papers if p.get("arxiv_id")]
    if not arxiv_papers:
        LOG.info("No arXiv papers in the selected set; skipping summarization.")
        return []

    program, _ = _get_summarizer()
    LOG.info("Summarizing %d arXiv papers via %s → %s", len(arxiv_papers), program, output_dir)

    async def _run_one(paper: dict) -> dict:
        arxiv_id = paper["arxiv_id"]
        title = paper.get("title", "untitled")
        md_name = _sanitize_filename(title)
        md_path = output_dir / md_name

        # If already summarized and has content, skip
        has_content, content_length = _check_md_content(md_path)
        if has_content:
            LOG.info("Skip %s — already summarized at %s (%d chars)", title, md_path, content_length)
            return {
                "title": title,
                "arxiv_id": arxiv_id,
                "md_path": str(md_path),
                "success": True,
                "skipped": True,
                "has_content": True,
                "content_length": content_length,
            }

        arxiv_url = f"https://arxiv.org/abs/{arxiv_id}"
        prompt = f"使用arxiv2md-summarize skill 总结这篇论文: {arxiv_url}, 总结的md文件以论文title命名,保存在{output_dir} 中"

        program, base_args = _get_summarizer()
        LOG.info("Summarizing: %s → %s via %s", title, md_path, program)
        try:
            cmd = [program, *base_args, prompt]
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            # Wait indefinitely (no timeout)
            stdout, stderr = await proc.communicate()
            if proc.returncode != 0:
                err = stderr.decode("utf-8", errors="replace")[:500]
                LOG.error("%s failed for %s (rc=%d): %s", program, title, proc.returncode, err)
                return {
                    "title": title,
                    "arxiv_id": arxiv_id,
                    "md_path": str(md_path),
                    "success": False,
                    "has_content": False,
                    "content_length": 0,
                    "error": err,
                }

            # After the summarizer returns, check whether the MD file was actually written and has content
            has_content, content_length = _check_md_content(md_path)
            if has_content:
                LOG.info("✓ Summarized: %s (%d chars)", md_path, content_length)
            else:
                LOG.warning("%s returned OK but %s is empty or missing", program, md_path)
            return {
                "title": title,
                "arxiv_id": arxiv_id,
                "md_path": str(md_path),
                "success": True,
                "has_content": has_content,
                "content_length": content_length,
            }
        except Exception as exc:
            LOG.error("%s exception for %s: %s", program, title, exc)
            return {
                "title": title,
                "arxiv_id": arxiv_id,
                "md_path": str(md_path),
                "success": False,
                "has_content": False,
                "content_length": 0,
                "error": str(exc),
            }

    semaphore = asyncio.Semaphore(concurrency)

    async def _bounded(paper: dict) -> dict:
        async with semaphore:
            return await _run_one(paper)

    results = await asyncio.gather(*[_bounded(p) for p in arxiv_papers])
    succeeded = sum(1 for r in results if r["success"] and r.get("has_content"))
    LOG.info("Summarization done: %d/%d have content", succeeded, len(arxiv_papers))
    return results

#!/usr/bin/env python3
"""Generate clean, image-rich Markdown from an arXiv paper for LLM summarization.

Usage:
    python summarize_paper.py <arxiv_id> [--output <path>]

Example:
    python summarize_paper.py 2501.11120
    python summarize_paper.py https://arxiv.org/abs/2501.11120 -o out.md
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def _import_arxiv2md():
    """Try to import arxiv2md; add common src paths if needed."""
    try:
        from arxiv2md import ingest_paper_sync
        return ingest_paper_sync
    except ImportError:
        # Try project-relative src paths
        cwd = Path.cwd()
        for rel in ("src", "../src", "../../src"):
            p = (cwd / rel).resolve()
            if p.exists() and str(p) not in sys.path:
                sys.path.insert(0, str(p))
        from arxiv2md import ingest_paper_sync
        return ingest_paper_sync


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Convert arXiv paper to Markdown with embedded image references."
    )
    parser.add_argument("arxiv_id", help="arXiv ID or URL (e.g. 2501.11120)")
    parser.add_argument(
        "-o",
        "--output",
        default='/tmp',
        help="Output file path (default: <arxiv_id>.md in /tmp directory)",
    )
    parser.add_argument(
        "--keep-refs",
        action="store_true",
        help="Keep references section (default: truncate to save context)",
    )
    parser.add_argument(
        "--keep-toc",
        action="store_true",
        help="Keep table of contents (default: remove)",
    )
    args = parser.parse_args()

    try:
        ingest_paper_sync = _import_arxiv2md()
    except ImportError as exc:
        print(
            f"Error: arxiv2md package not found. {exc}\n"
            "Install the project first: pip install -e /path/to/arxiv2md",
            file=sys.stderr,
        )
        sys.exit(1)

    result = ingest_paper_sync(
        args.arxiv_id,
        remove_refs=not args.keep_refs,
        remove_toc=not args.keep_toc,
        remove_inline_citations=True,
    )
    safe_id = args.arxiv_id.replace("https://", "").replace("http://", "")
    safe_id = safe_id.replace("arxiv.org/abs/", "").replace("arxiv.org/html/", "")
    safe_id = safe_id.replace("/", "_")
    # Derive output path
    if args.output:
        output_path = Path(args.output) / f"{safe_id}.md"
    else:
        output_path = Path("/tmp") / f"{safe_id}.md"

    output_path.write_text(result.content, encoding="utf-8")
    print(f"Saved Markdown to: {output_path.resolve()}")
    print("-" * 40)
    print(result.summary)


if __name__ == "__main__":
    main()

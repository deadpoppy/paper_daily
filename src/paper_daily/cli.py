"""Command-line interface."""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from paper_daily.config import load_config
from paper_daily.database import PaperDatabase
from paper_daily.output import format_console, format_markdown
from paper_daily.pipeline import run


def _setup_logging(verbose: bool = False):
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        stream=sys.stderr,
    )
    http_level = logging.INFO if verbose else logging.WARNING
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(http_level)


def cmd_run(args: argparse.Namespace) -> int:
    _setup_logging(args.verbose or args.debug)
    env_path = Path(args.env) if args.env else None
    cfg = load_config(env_path)

    # Override via CLI
    if args.top_n is not None:
        cfg.top_n = args.top_n
    if args.days is not None:
        cfg.days_back = args.days
    if args.data_dir is not None:
        cfg.data_dir = Path(args.data_dir).resolve()
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
    if args.trim_ratio is not None:
        cfg.trim_ratio = args.trim_ratio
    if args.debug:
        cfg.debug = True

    papers = run(cfg)
    return 0 if papers else 1


def cmd_history(args: argparse.Namespace) -> int:
    _setup_logging(args.verbose)
    cfg = load_config()
    if args.data_dir:
        cfg.data_dir = Path(args.data_dir).resolve()
    db = PaperDatabase(cfg.data_dir / "paper_daily.db")

    if args.days:
        recs = db.get_recent_recommendations(days=args.days)
    else:
        recs = db.get_all_recommendations(limit=args.limit)

    if not recs:
        print("No recommendations found.")
        return 0

    # Reconstruct paper dicts for formatting
    papers = []
    for r in recs:
        papers.append({
            "title": r["title"],
            "year": "",
            "venue": "",
            "reason_zh": r["reason_zh"],
            "url": r["url"] or (f"https://doi.org/{r['doi']}" if r.get("doi") else "") or (f"https://arxiv.org/abs/{r['arxiv_id']}" if r.get("arxiv_id") else ""),
            "doi": r.get("doi", ""),
            "arxiv_id": r.get("arxiv_id", ""),
        })

    if args.format == "markdown":
        print(format_markdown(papers))
    else:
        for p in papers:
            print(f"[{p['reason_zh'][:20]}...] {p['title']} | {p['url']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="paper-daily",
        description="Daily AI paper recommender: multi-source search, dedup, rank, review, archive.",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose logging")
    parser.add_argument("--env", type=str, default=None, help="Path to .env file")
    parser.add_argument("--data-dir", type=str, default=None, help="Data directory")
    sub = parser.add_subparsers(dest="command", required=True)

    # run
    run_p = sub.add_parser("run", help="Run the daily recommendation pipeline")
    run_p.add_argument("-n", "--top-n", type=int, default=None, help="Number of papers to recommend")
    run_p.add_argument("-d", "--days", type=int, default=None, help="Look back N days (default: 180)")
    run_p.add_argument("--trim-ratio", type=float, default=0.01, help="Trim bottom N ratio before assessment, e.g. 0.2 for 20%% (default: 0.2)")
    run_p.add_argument("--debug", action="store_true", help="Enable debug output: searched papers, LLM requests/responses, and filtering details")
    run_p.set_defaults(func=cmd_run)

    # history
    hist_p = sub.add_parser("history", help="Show past recommendations")
    hist_p.add_argument("--days", type=int, default=None, help="Show last N days")
    hist_p.add_argument("--limit", type=int, default=50, help="Max records")
    hist_p.add_argument("-f", "--format", choices=["console", "markdown"], default="console")
    hist_p.set_defaults(func=cmd_history)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

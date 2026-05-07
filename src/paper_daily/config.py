"""Configuration management."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


@dataclass
class Topic:
    key: str
    label: str
    keywords: list[str]
    weight: float = 1.0


@dataclass
class Config:
    data_dir: Path
    topics: list[Topic]
    top_n: int = 10
    max_results_per_source: int = 50
    days_back: int = 180
    openalex_email: str | None = None
    # Unified LLM API (Anthropic-compatible) for both academic assessment & Chinese reasons
    academic_value_url: str | None = None
    academic_value_api_key: str | None = None
    academic_value_backup_api_key: str | None = None
    academic_value_model: str = "MiniMax-M2.7"
    academic_value_concurrency: int = 20
    # Ranking weights (5-dimension, uniform)
    w_relevance: float = 0.20
    w_recency: float = 0.20
    w_impact: float = 0.20
    w_novelty: float = 0.20
    w_academic_value: float = 0.20
    trim_ratio: float = 0.20  # trim bottom N%% before academic assessment
    debug: bool = False
    # Search sources: comma-separated list in env, e.g. "arxiv,semantic_scholar"
    sources: list[str] = field(default_factory=lambda: ["arxiv", "semantic_scholar"])
    # Whether to resolve non-arXiv papers back to arXiv via extra API calls
    resolve_arxiv: bool = False


def load_config(env_path: Path | None = None) -> Config:
    if env_path and env_path.exists():
        load_dotenv(env_path)
    else:
        # try default locations
        for p in [Path(".env"), Path.home() / ".paper-daily" / ".env"]:
            if p.exists():
                load_dotenv(p)
                break

    data_dir = Path(os.getenv("PAPER_DAILY_DATA_DIR", "./data")).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)

    topics = _default_topics()

    sources_str = os.getenv("PAPER_DAILY_SOURCES", "arxiv,semantic_scholar")
    sources = [s.strip() for s in sources_str.split(",") if s.strip()]

    return Config(
        data_dir=data_dir,
        topics=topics,
        top_n=int(os.getenv("PAPER_DAILY_TOP_N", "10")),
        max_results_per_source=int(os.getenv("PAPER_DAILY_MAX_RESULTS", "50")),
        days_back=int(os.getenv("PAPER_DAILY_DAYS_BACK", "180")),
        openalex_email=os.getenv("OPENALEX_EMAIL") or None,
        academic_value_url=os.getenv("ACADEMIC_VALUE_URL") or None,
        academic_value_api_key=os.getenv("ACADEMIC_VALUE_API_KEY") or None,
        academic_value_backup_api_key=os.getenv("ACADEMIC_VALUE_BACKUP_API_KEY") or None,
        academic_value_model=os.getenv("ACADEMIC_VALUE_MODEL", "MiniMax-M2.7"),
        academic_value_concurrency=int(os.getenv("ACADEMIC_VALUE_CONCURRENCY", "20")),
        w_relevance=float(os.getenv("W_RELEVANCE", "0.20")),
        w_recency=float(os.getenv("W_RECENCY", "0.20")),
        w_impact=float(os.getenv("W_IMPACT", "0.20")),
        w_novelty=float(os.getenv("W_NOVELTY", "0.20")),
        w_academic_value=float(os.getenv("W_ACADEMIC_VALUE", "0.20")),
        trim_ratio=float(os.getenv("PAPER_DAILY_TRIM_RATIO", "0.20")),
        debug=os.getenv("PAPER_DAILY_DEBUG", "false").lower() in ("1", "true", "yes"),
        sources=sources,
        resolve_arxiv=os.getenv("PAPER_DAILY_RESOLVE_ARXIV", "false").lower() in ("1", "true", "yes"),
    )


def _default_topics() -> list[Topic]:
    """Default AI-related search topics (broad coverage)."""
    return [
        # Topic("dl_theory", "Deep Learning Theory", ["neural network theory", "optimization landscape", "deep learning theory", "representation learning", "generalization"]),
        # Topic("foundation_model", "Foundation Models", ["foundation model", "pre-training", "scaling law", "emergent ability", "model adaptation"]),
        # Topic("llm", "Large Language Models", ["large language model", "LLM", "transformer", "reasoning", "in-context learning"]),
        # Topic("vision", "Computer Vision", ["computer vision", "diffusion model", "image generation", "segmentation", "object detection"]),
        # Topic("rl", "Reinforcement Learning", ["reinforcement learning", "RLHF", "PPO", "Q-learning", "policy gradient"]),
        # Topic("multimodal", "Multimodal Learning", ["multimodal", "vision-language model", "cross-modal", "audio-visual"]),
        # Topic("agent", "AI Agents", ["AI agent", "autonomous agent", "tool use", "planning", "multi-agent"]),
        # Topic("efficiency", "Efficiency & Systems", ["model efficiency", "quantization", "pruning", "distillation", "inference optimization"]),
        # Topic("genai", "Generative AI", ["generative AI", "text-to-image", "text-to-video", "flow model", "generative model"]),

        # Topic("embodied", "Embodied AI", ["embodied AI", "robot learning", "manipulation", "humanoid robot", "locomotion"]),
        # Topic("autonomous_driving", "Autonomous Driving", ["autonomous driving", "self-driving", "end-to-end driving", "motion planning"]),
        # Topic("world_model", "World Models", ["world model", "environment model", "predictive model", "model-based RL"]),
        # Topic("slam", "SLAM", ["SLAM", "simultaneous localization and mapping", "visual odometry", "3D reconstruction"]),
        # Topic("end_to_end", "End-to-End Learning", ["end-to-end learning", "end-to-end system", "end-to-end training", "end-to-end optimization"]),

        Topic("speech_tokenizer", "Speech Tokenization & Codec", [
            "LM-aligned speech tokenizer",
            "text-guided speech tokenization",
            "text-to-speech tokenizer",
            "semantic speech codec",
            "joint speech-text autoencoder",
            "multi-modal discrete autoencoder speech text",
            "discrete speech representation",
            "speech language model tokenizer",
        ]),
    ]

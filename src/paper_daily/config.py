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
    exclude_keywords: list[str] = field(default_factory=list)
    exclude_title_keywords: list[str] = field(default_factory=list)


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
    academic_value_threshold: float = 0.4  # filter out papers with normalized score < threshold
    # Ranking weights (5-dimension, uniform)
    w_relevance: float = 0.20
    w_recency: float = 0.20
    w_impact: float = 0.20
    w_academic_value: float = 0.20
    trim_ratio: float = 0.20  # trim bottom N%% before academic assessment
    debug: bool = False
    # Search sources: comma-separated list in env, e.g. "arxiv,semantic_scholar"
    sources: list[str] = field(default_factory=lambda: ["arxiv", "semantic_scholar"])
    # Whether to resolve non-arXiv papers back to arXiv via extra API calls
    resolve_arxiv: bool = False


def _getenv_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f"Invalid integer value for {name}: {raw!r}") from None


def _getenv_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        raise ValueError(f"Invalid float value for {name}: {raw!r}") from None


def _getenv_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.lower() in ("1", "true", "yes")


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
        top_n=_getenv_int("PAPER_DAILY_TOP_N", 10),
        max_results_per_source=_getenv_int("PAPER_DAILY_MAX_RESULTS", 50),
        days_back=_getenv_int("PAPER_DAILY_DAYS_BACK", 180),
        openalex_email=os.getenv("OPENALEX_EMAIL") or None,
        academic_value_url=os.getenv("ACADEMIC_VALUE_URL") or None,
        academic_value_api_key=os.getenv("ACADEMIC_VALUE_API_KEY") or None,
        academic_value_backup_api_key=os.getenv("ACADEMIC_VALUE_BACKUP_API_KEY") or None,
        academic_value_model=os.getenv("ACADEMIC_VALUE_MODEL", "MiniMax-M2.7"),
        academic_value_concurrency=_getenv_int("ACADEMIC_VALUE_CONCURRENCY", 20),
        academic_value_threshold=_getenv_float("ACADEMIC_VALUE_THRESHOLD", 0.4),
        w_relevance=_getenv_float("W_RELEVANCE", 0.20),
        w_recency=_getenv_float("W_RECENCY", 0.20),
        w_impact=_getenv_float("W_IMPACT", 0.20),
        w_academic_value=_getenv_float("W_ACADEMIC_VALUE", 0.20),
        trim_ratio=_getenv_float("PAPER_DAILY_TRIM_RATIO", 0.20),
        debug=_getenv_bool("PAPER_DAILY_DEBUG", False),
        sources=sources,
        resolve_arxiv=_getenv_bool("PAPER_DAILY_RESOLVE_ARXIV", False),
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
        # Topic("autonomous_driving_e2e", "End-to-End Autonomous Driving", [
        #     "end-to-end autonomous driving",
        #     "end-to-end self-driving",
        #     "end-to-end driving",
        #     "end-to-end driving policy",
        #     "end-to-end driving planning",
        #     "one-stage end-to-end autonomous driving",
        #     "two-stage end-to-end autonomous driving",
        #     "perception-to-trajectory autonomous driving",
        #     "perception-to-control autonomous driving",
        #     "vision-to-trajectory autonomous driving",
        #     "camera-to-trajectory autonomous driving",
        #     "sensor-to-trajectory autonomous driving",
        #     "joint perception and planning autonomous driving",
        #     "joint perception prediction and planning autonomous driving",
        #     "planning-oriented autonomous driving",
        #     "closed-loop end-to-end autonomous driving",
        #     "diffusion-based end-to-end autonomous driving",
        #     "diffusion policy for autonomous driving",
        # ], exclude_keywords=[
        #     "vision-language-action",
        #     "vision language action",
        #     "vision-language model",
        #     "vision language model",
        #     "large language model",
        #     "language model",
        #     "multimodal language model",
        #     "language-guided driving",
        #     "language conditioned driving",
        #     "language-conditioned driving",
        #     "natural language driving",
        #     "autonomous driving agent",
        #     "VLA",
        #     "VLM",
        # ], exclude_title_keywords=[
        #     "autonomous driving simulation",
        #     "driving simulation",
        #     "test-time verifier",
        #     "trajectory evaluator",
        # ]),
        Topic("embodied_agent", "Embodied Agent Capabilities", [
            # Validated high-level agent capabilities in physical or simulated environments.
            # Avoid the broad "embodied agent" phrase: it exhausts the result cap before
            # capability-specific papers can be returned.
            "embodied planning",
            "embodied agent planning",
            "embodied agent task planning",
            "embodied reasoning",
            "embodied agent memory",
            "memory-augmented embodied agent",
            "embodied agent navigation",
            "long-horizon embodied agents",
            "self-evolving embodied agents",
            "multi-agent embodied planning",
        ], exclude_keywords=[
            # Keep driving VLA papers in the dedicated driving topic instead.
            "autonomous driving",
            "self-driving",
            "autonomous vehicle",
        ], exclude_title_keywords=[
            # "Embodied agent" is also a term of art for virtual conversational avatars.
            "embodied conversational agent",
            "conversational embodied agent",
            "virtual human",
            "digital human",
            "uncanny valley",
        ]),
        # Topic("world_model", "World Models", [
        #     "world model",
        #     "environment model",
        #     "predictive model",
        #     "model-based RL",
        #     "forward model",
        #     "dynamics model",
        #     "latent dynamics",
        #     "video prediction",
        #     "next frame prediction",
        #     "imagination-based learning",
        #     "planning with learned models",
        #     "dreamer",
        #     "planet agent",
        #     "RSSM",
        #     "recurrent state space model",
        #     "state representation learning",
        #     "latent state representation",
        #     "temporal consistency",
        #     "self-supervised video learning",
        #     "contrastive dynamics learning",
        #     "bisimulation",
        #     "world models for planning",
        #     "learned world models",
        #     "visual world model",
        #     "model-based planning",
        #     "sample efficiency reinforcement learning",
        #     "imagined rollouts",
        #     "model dynamics learning",
        #     "end-to-end world models",
        #     "probabilistic world model",
        # ]),
        # Topic("embodied_agent", "Embodied Interactive Agents", [
        #     "embodied LLM agent",
        #     "embodied agent task planning",
        #     "interactive embodied agent",
        #     "embodied reasoning",
        #     "long-horizon robotic tasks",
        #     "task and motion planning",
        #     "closed-loop robot control",
        #     "robot feedback loop",
        #     "robot failure recovery",
        #     "robot replanning",
        #     "self-correcting robot",
        #     "constraint-aware robot planning",
        #     "agentic robot planning",
        #     "memory-augmented embodied agent",
        #     "human-robot interaction planning",
        # ]),
        # Topic("world_action_model", "World-Action Models", [
        #     "world action model",
        #     "world action models",
        #     "world-action model",
        #     "world-action models",
        #     "world action modeling",
        #     "world-action modeling",
        #     "World Action Models are Zero-shot Policies",
        #     "Fast-WAM",
        #     "video-action world model",
        #     "joint video-action",
        #     "world-language-action model",
        #     "world-value-action model",
        #     "world-awareness-action model",
        #     "latent world-action model",
        #     "world-action interactive model",
        # ]),
        # Topic("optimizer", "Deep Learning Optimizers", [
        #     "deep learning optimizer",
        #     "deep learning optimizers",
        #     "neural network optimizer",
        #     "neural network optimizers",
        #     "optimizer for neural networks",
        #     "optimizer for deep learning",
        #     "training optimizer for neural networks",
        #     "LLM optimizer",
        #     "LLM pre-training optimizer",
        #     "large language model optimizer",
        #     "stochastic gradient optimizer",
        #     "adaptive gradient optimizer",
        #     "Adam optimizer",
        #     "AdamW optimizer",
        #     "Muon optimizer",
        #     "Lion optimizer",
        #     "Sophia optimizer",
        #     "Shampoo optimizer",
        #     "Adafactor optimizer",
        #     "SOAP optimizer",
        #     "K-FAC optimizer",
        #     "orthogonalization optimizer",
        # ]),
        # Topic("slam", "SLAM", ["SLAM", "simultaneous localization and mapping", "visual odometry", "3D reconstruction"]),
        # Topic("end_to_end", "End-to-End Learning", ["end-to-end learning", "end-to-end system", "end-to-end training", "end-to-end optimization"]),

        # Topic("speech_tokenizer", "Speech Tokenization & Codec", [
        #     "LM-aligned speech tokenizer",
        #     "text-guided speech tokenization",
        #     "text-to-speech tokenizer",
        #     "semantic speech codec",
        #     "joint speech-text autoencoder",
        #     "multi-modal discrete autoencoder speech text",
        #     "discrete speech representation",
        #     "speech language model tokenizer",
        #     "speech tokenizer",
        # ]),
        # Topic("vla", "Vision-Language-Action Models", [
        #     "vision-language-action model",
        #     "VLA robot policy",
        #     "VLN",
        #     "vision language navigation",
        #     "VLA",
        #     "language-conditioned robot policy",
        #     "generalist robot manipulation policy",
        #     "multimodal robot action prediction",
        #     "embodied vision language model",
        #     "robot foundation model",
        #     "language-guided robot manipulation",
        #     "vision language robot learning",
        # ]),
    ]

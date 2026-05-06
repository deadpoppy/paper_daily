"""Lightweight semantic relevance scoring using sentence-transformers.

Runs entirely on CPU for cross-platform compatibility.
Falls back gracefully if the model cannot be loaded.
"""
from __future__ import annotations

import logging
import os
from typing import Any

LOG = logging.getLogger("paper_daily.semantic_ranker")

# Model downloaded automatically on first use (~80 MB).
_DEFAULT_MODEL = "sentence-transformers/all-MiniLM-L6-v2"


class SemanticRanker:
    """Singleton wrapper for a lightweight sentence-transformer model.

    The model is loaded lazily on first use and cached for the process lifetime.
    All inference happens on CPU regardless of GPU availability.
    """

    _instance: SemanticRanker | None = None

    def __new__(cls) -> SemanticRanker:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._model = None
            cls._instance._topic_cache: dict[str, Any] = {}
            cls._instance._model_name = os.getenv("PAPER_DAILY_EMBED_MODEL", _DEFAULT_MODEL)
        return cls._instance

    @property
    def available(self) -> bool:
        """True if the embedding model is ready for inference."""
        if self._model is None:
            self._load_model()
        return self._model is not None

    def _load_model(self) -> None:
        """Attempt to load the sentence-transformer model on CPU."""
        try:
            import logging as std_logging

            from sentence_transformers import SentenceTransformer

            # Suppress noisy transformers logging during model load.
            for noisy in ("transformers", "accelerate", "safetensors"):
                std_logging.getLogger(noisy).setLevel(std_logging.ERROR)

            self._model = SentenceTransformer(self._model_name, device="cpu")
            try:
                dim = self._model.get_embedding_dimension()
            except AttributeError:
                dim = self._model.get_sentence_embedding_dimension()
            LOG.info(
                "Loaded semantic ranking model '%s' on CPU (dim=%d)",
                self._model_name,
                dim,
            )
        except Exception as exc:
            LOG.warning(
                "Failed to load sentence-transformers model '%s': %s. "
                "Falling back to lexical relevance scoring.",
                self._model_name,
                exc,
            )
            self._model = None

    def _encode_topics(self, topic_keywords: list[list[str]]) -> list[Any]:
        """Return cached or freshly-computed topic embeddings."""
        if not self.available:
            return []

        topic_texts = [" ".join(kws) for kws in topic_keywords]
        new_texts: list[str] = []
        new_indices: list[int] = []

        for i, text in enumerate(topic_texts):
            if text not in self._topic_cache:
                new_texts.append(text)
                new_indices.append(i)

        if new_texts:
            embeddings = self._model.encode(
                new_texts,
                convert_to_tensor=True,
                show_progress_bar=False,
                normalize_embeddings=True,
            )
            for idx, emb in zip(new_indices, embeddings):
                self._topic_cache[topic_texts[idx]] = emb

        return [self._topic_cache[text] for text in topic_texts]

    def score_papers(self, papers: list[dict], topic_keywords: list[list[str]]) -> list[float] | None:
        """Compute semantic relevance scores for a batch of papers.

        Returns a list of floats in [0, 1] aligned with *papers*, or *None*
        if the model is unavailable.
        """
        if not self.available or not papers:
            return None

        topic_embs = self._encode_topics(topic_keywords)
        if not topic_embs:
            return None

        # Build paper texts (title + abstract, truncated to keep memory low).
        paper_texts: list[str] = []
        for p in papers:
            title = (p.get("title") or "").strip()
            abstract = (p.get("abstract") or "").strip()
            # Truncate abstract to first 800 chars to speed up encoding.
            text = f"{title} {abstract[:800]}".strip()
            paper_texts.append(text)

        try:
            import torch

            paper_embs = self._model.encode(
                paper_texts,
                convert_to_tensor=True,
                show_progress_bar=False,
                normalize_embeddings=True,
            )

            # Stack topic embeddings -> (n_topics, dim)
            topic_tensor = torch.stack(topic_embs)

            # Cosine similarity = dot product because vectors are normalized.
            # Result shape: (n_papers, n_topics)
            sim_matrix = torch.mm(paper_embs, topic_tensor.T)
            max_scores = sim_matrix.max(dim=1).values

            # Clamp to [0, 1] and convert to Python floats.
            scores = torch.clamp(max_scores, min=0.0, max=1.0).cpu().tolist()
            return scores
        except Exception as exc:
            LOG.warning("Semantic scoring failed: %s. Falling back to lexical scoring.", exc)
            return None


def get_semantic_ranker() -> SemanticRanker:
    """Return the global SemanticRanker singleton."""
    return SemanticRanker()

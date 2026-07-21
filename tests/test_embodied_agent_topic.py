"""Regression tests for the embodied-agent topic."""
from __future__ import annotations

import unittest

from paper_daily.config import _default_topics
from paper_daily.pipeline import _matches_excluded_keyword


class EmbodiedAgentTopicTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.topic = next(
            topic for topic in _default_topics() if topic.key == "embodied_agent"
        )

    def test_covers_embodied_agent_capabilities(self) -> None:
        self.assertTrue(
            {
                "embodied planning",
                "embodied agent planning",
                "embodied agent task planning",
                "embodied reasoning",
                "embodied agent memory",
                "embodied agent navigation",
                "long-horizon embodied agents",
                "self-evolving embodied agents",
                "multi-agent embodied planning",
            }.issubset(self.topic.keywords)
        )

    def test_removes_unvalidated_or_overly_broad_queries(self) -> None:
        self.assertTrue(
            {
                "embodied agent",
                "vision-language-action",
                "language-conditioned robot policy",
                "multimodal robot policy",
                "robot foundation model",
                "closed-loop robot interaction",
                "world model for robot control",
                "embodied agent spatial reasoning",
                "embodied agent spatial cognition",
                "embodied agent exploration",
                "embodied agent adaptation",
                "embodied agent self-reflection",
                "embodied agent self-improvement",
                "embodied agent failure recovery",
                "embodied agent replanning",
            }.isdisjoint(self.topic.keywords)
        )

    def test_excludes_driving_papers(self) -> None:
        paper = {
            "title": "A Vision-Language-Action Driving Policy",
            "abstract": "We develop an embodied agent for autonomous driving.",
        }
        self.assertTrue(_matches_excluded_keyword(paper, self.topic.exclude_keywords))

    def test_excludes_conversational_avatars_by_title(self) -> None:
        paper = {
            "title": "The Uncanny Valley Effect in Embodied Conversational Agents",
            "abstract": "A study of social interaction with virtual avatars.",
        }
        self.assertTrue(
            _matches_excluded_keyword(
                paper, self.topic.exclude_title_keywords, fields=("title",)
            )
        )

    def test_keeps_simulated_embodied_tasks(self) -> None:
        paper = {
            "title": "Memory-Guided Embodied Agent Navigation",
            "abstract": "The agent plans and acts in a simulated household environment.",
        }
        self.assertFalse(_matches_excluded_keyword(paper, self.topic.exclude_keywords))
        self.assertFalse(
            _matches_excluded_keyword(
                paper, self.topic.exclude_title_keywords, fields=("title",)
            )
        )


if __name__ == "__main__":
    unittest.main()

"""Regression tests for the end-to-end autonomous-driving topic."""
from __future__ import annotations

import unittest

from paper_daily.config import _default_topics
from paper_daily.pipeline import _matches_excluded_keyword


class EndToEndDrivingTopicTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.topic = next(
            topic
            for topic in _default_topics()
            if topic.key == "autonomous_driving_e2e"
        )

    def test_keeps_end_to_end_and_diffusion_queries(self) -> None:
        self.assertTrue(
            {
                "end-to-end autonomous driving",
                "one-stage end-to-end autonomous driving",
                "two-stage end-to-end autonomous driving",
                "perception-to-trajectory autonomous driving",
                "joint perception and planning autonomous driving",
                "diffusion-based end-to-end autonomous driving",
            }.issubset(self.topic.keywords)
        )

    def test_removes_broad_non_end_to_end_queries(self) -> None:
        self.assertTrue(
            {
                "autonomous driving trajectory prediction",
                "autonomous driving foundation model",
                "autonomous driving benchmark",
                "diffusion model for autonomous driving",
                "end-to-end planning and control",
                "end-to-end motion planning and control",
            }.isdisjoint(self.topic.keywords)
        )

    def test_excludes_vision_language_action_papers(self) -> None:
        paper = {
            "title": "A Vision-Language-Action Driving Agent",
            "abstract": "We use a VLA for autonomous driving.",
        }
        self.assertTrue(
            _matches_excluded_keyword(paper, self.topic.exclude_keywords)
        )

    def test_keeps_end_to_end_diffusion_driving_papers(self) -> None:
        paper = {
            "title": "Diffusion-Based End-to-End Autonomous Driving",
            "abstract": "A closed-loop end-to-end driving policy from sensors to trajectories.",
        }
        self.assertFalse(
            _matches_excluded_keyword(paper, self.topic.exclude_keywords)
        )

    def test_excludes_simulation_only_when_named_in_title(self) -> None:
        simulation_paper = {
            "title": "Closed-Loop Autonomous Driving Simulation",
            "abstract": "A simulator for evaluating autonomous driving systems.",
        }
        driving_paper = {
            "title": "A Closed-Loop End-to-End Driving Policy",
            "abstract": "We evaluate the policy in autonomous driving simulation.",
        }
        self.assertTrue(
            _matches_excluded_keyword(
                simulation_paper,
                self.topic.exclude_title_keywords,
                fields=("title",),
            )
        )
        self.assertFalse(
            _matches_excluded_keyword(
                driving_paper,
                self.topic.exclude_title_keywords,
                fields=("title",),
            )
        )


if __name__ == "__main__":
    unittest.main()

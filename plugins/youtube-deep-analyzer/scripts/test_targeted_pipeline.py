from __future__ import annotations

import unittest

from media_pipeline import (
    MediaPipelineError,
    _normalize_visual_ranges,
    _semantic_ocr_decision,
    _signature_distance,
    _unit_tokens,
    _visual_signature,
)


def visual_range(kind: str = "onscreen_text") -> dict:
    return {
        "range_index": 0,
        "start_seconds": 10.0,
        "end_seconds": 20.0,
        "question": "What appears on screen?",
        "question_kind": kind,
        "transcript_context": "Revenue is 35 percent.",
        "known_text": "Revenue",
        "priority": 3,
    }


def frame(**overrides) -> dict:
    value = {
        "selection_reason": "visual-change",
        "phash_distance": 0.1,
        "aligned_mad": 0.02,
        "edge_change": 0.1,
    }
    value.update(overrides)
    return value


class TargetedPipelineTests(unittest.TestCase):
    def test_normalizes_transcript_ranges(self) -> None:
        result = _normalize_visual_ranges(
            [
                {
                    "start_seconds": 10,
                    "end_seconds": 20,
                    "question": "Read the correction",
                    "question_kind": "onscreen_text",
                    "transcript_context": "The year was 2024",
                }
            ],
            duration_seconds=600,
            allow_whole_video_visual_pass=False,
        )
        self.assertEqual(result[0]["start_seconds"], 10.0)
        self.assertEqual(result[0]["question_kind"], "onscreen_text")

    def test_rejects_overlapping_ranges(self) -> None:
        with self.assertRaises(MediaPipelineError):
            _normalize_visual_ranges(
                [
                    {"start_seconds": 1, "end_seconds": 8, "question": "a", "question_kind": "chart"},
                    {"start_seconds": 7, "end_seconds": 9, "question": "b", "question_kind": "chart"},
                ],
                duration_seconds=100,
                allow_whole_video_visual_pass=False,
            )

    def test_rejects_unapproved_whole_video_pass(self) -> None:
        with self.assertRaises(MediaPipelineError):
            _normalize_visual_ranges(
                [{"start_seconds": 0, "end_seconds": 600, "question": "all", "question_kind": "unknown"}],
                duration_seconds=600,
                allow_whole_video_visual_pass=False,
            )

    def test_plain_text_can_resolve_without_image_model(self) -> None:
        decision = _semantic_ocr_decision(
            frame(),
            visual_range("onscreen_text"),
            {"text": "Revenue 35%", "average_confidence": 0.92, "observation_count": 1},
            None,
        )
        self.assertTrue(decision["resolved_by_local_ocr"])
        self.assertFalse(decision["image_model_recommended"])

    def test_chart_relationship_still_needs_image_model(self) -> None:
        decision = _semantic_ocr_decision(
            frame(),
            visual_range("chart"),
            {"text": "Revenue 35%", "average_confidence": 0.95, "observation_count": 1},
            None,
        )
        self.assertFalse(decision["resolved_by_local_ocr"])
        self.assertTrue(decision["image_model_recommended"])

    def test_ocr_failure_falls_back_to_image_model(self) -> None:
        decision = _semantic_ocr_decision(frame(), visual_range(), None, "OCR unavailable")
        self.assertTrue(decision["image_model_recommended"])

    def test_near_duplicate_is_suppressed_after_ocr(self) -> None:
        decision = _semantic_ocr_decision(
            frame(phash_distance=0.03125, aligned_mad=0.006, edge_change=0.04),
            visual_range("chart"),
            {"text": "Revenue 35 percent", "average_confidence": 0.95, "observation_count": 1},
            None,
        )
        self.assertTrue(decision["near_duplicate"])
        self.assertFalse(decision["image_model_recommended"])

    def test_signature_distance_exposes_staged_metrics(self) -> None:
        black = bytes([0, 0, 0] * 1024)
        white = bytes([255, 255, 255] * 1024)
        result = _signature_distance(_visual_signature(white), _visual_signature(black))
        self.assertIn("aligned_mad", result)
        self.assertIn("edge_change", result)
        self.assertIn("changed_area", result)

    def test_units_are_detected_when_attached_to_numbers(self) -> None:
        self.assertEqual(_unit_tokens("100kg, 250ms, 35%, 5000원"), {"kg", "ms", "%", "원"})


if __name__ == "__main__":
    unittest.main()

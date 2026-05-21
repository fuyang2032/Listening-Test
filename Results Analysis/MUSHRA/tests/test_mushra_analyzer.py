#!/usr/bin/env python3
"""Regression test for the MUSHRA analyzer.

The fixture intentionally stores raw subjective values on a 0-1 scale. The
analyzer must normalize them linearly to the ITU-R BS.1534 0-100 score range
before screening listeners and producing boxplot outliers.
"""

from __future__ import annotations

import csv
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "mushra_analyzer.py"


def load_analyzer():
    spec = importlib.util.spec_from_file_location("mushra_analyzer", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load {SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def write_fixture(path: Path) -> None:
    subjects = [f"S{i:02d}" for i in range(1, 9)]
    items = [f"seq{i}" for i in range(1, 5)]

    def codec_a_score(subject_index: int, item_index: int) -> float:
        return 0.72 + 0.025 * ((subject_index * 2 + item_index * 3) % 9)

    def codec_b_score(subject_index: int, item_index: int) -> float:
        return 0.56 + 0.035 * ((subject_index + item_index * 2) % 9)

    rows: list[dict[str, object]] = []
    for subject_index, subject in enumerate(subjects, 1):
        for item_index, item in enumerate(items, 1):
            hidden_ref = 0.93 + 0.015 * ((subject_index + item_index) % 5)
            anchor = 0.12 + 0.02 * ((subject_index + item_index) % 5)
            if subject == "S02" and item == "seq2":
                hidden_ref = 0.85
            if subject == "S03" and item == "seq3":
                anchor = 0.25

            codec_a = codec_a_score(subject_index, item_index)
            codec_b = codec_b_score(subject_index, item_index)
            if subject == "S08" and item == "seq4":
                codec_b = 0.15

            rows.extend(
                [
                    {
                        "subject": subject,
                        "item": item,
                        "condition": "HiddenRef",
                        "role": "hidden_reference",
                        "score": hidden_ref,
                    },
                    {
                        "subject": subject,
                        "item": item,
                        "condition": "Anchor",
                        "role": "anchor",
                        "score": anchor,
                    },
                    {
                        "subject": subject,
                        "item": item,
                        "condition": "CodecA",
                        "role": "system",
                        "score": round(codec_a, 4),
                    },
                    {
                        "subject": subject,
                        "item": item,
                        "condition": "CodecB",
                        "role": "system",
                        "score": round(codec_b, 4),
                    },
                ]
            )

    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=["subject", "item", "condition", "role", "score"]
        )
        writer.writeheader()
        writer.writerows(rows)


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


class MushraAnalyzerTest(unittest.TestCase):
    def test_itu1534_normalization_screening_and_boxplot_outliers(self) -> None:
        analyzer = load_analyzer()
        with tempfile.TemporaryDirectory(prefix="mushra-analyzer-test-") as temp:
            temp_path = Path(temp)
            input_path = temp_path / "mushra_raw_0_to_1.csv"
            output_dir = temp_path / "out"
            write_fixture(input_path)

            args = analyzer.build_parser().parse_args(
                [
                    str(input_path),
                    "--output-dir",
                    str(output_dir),
                    "--raw-score-min",
                    "0",
                    "--raw-score-max",
                    "1",
                ]
            )
            outputs = analyzer.run_analysis(args)

            all_scores = read_csv(outputs["all_scores_with_flags"])
            hidden_ref = next(
                row
                for row in all_scores
                if row["subject"] == "S01"
                and row["item"] == "seq1"
                and row["condition"] == "HiddenRef"
            )
            self.assertEqual(hidden_ref["raw_score"], "0.96")
            self.assertEqual(hidden_ref["score"], "96")

            listener_rows = {
                row["subject"]: row for row in read_csv(outputs["listener_screening"])
            }
            self.assertEqual(listener_rows["S02"]["excluded"], "True")
            self.assertIn("hidden_ref_fail_rate 25.00% > 15.00%", listener_rows["S02"]["exclusion_reason"])
            self.assertEqual(listener_rows["S03"]["excluded"], "True")
            self.assertIn("anchor_fail_rate 25.00% > 15.00%", listener_rows["S03"]["exclusion_reason"])
            for subject in ["S01", "S04", "S05", "S06", "S07", "S08"]:
                self.assertEqual(listener_rows[subject]["excluded"], "False")

            clean_system_scores = read_csv(outputs["clean_system_scores"])
            self.assertEqual(len(clean_system_scores), 48)
            self.assertNotIn("S02", {row["subject"] for row in clean_system_scores})
            self.assertNotIn("S03", {row["subject"] for row in clean_system_scores})

            codec_b_summary = next(
                row
                for row in read_csv(outputs["condition_summary_systems"])
                if row["condition"] == "CodecB"
            )
            self.assertEqual(codec_b_summary["outlier_count"], "1")
            self.assertEqual(codec_b_summary["outlier_values"], "15")

            outliers = read_csv(outputs["statistical_outliers"])
            self.assertTrue(
                any(
                    row["subject"] == "S08"
                    and row["item"] == "seq4"
                    and row["condition"] == "CodecB"
                    and row["score"] == "15"
                    for row in outliers
                )
            )

            boxplot_svg = outputs["boxplot"].read_text(encoding="utf-8")
            self.assertIn("<circle", boxplot_svg)
            self.assertIn("CodecB outlier: 15", boxplot_svg)

            conclusion = outputs["conclusion"].read_text(encoding="utf-8")
            self.assertIn("[0, 1] -> [0, 100]", conclusion)
            self.assertIn("剔除听音者数：2", conclusion)


if __name__ == "__main__":
    unittest.main()

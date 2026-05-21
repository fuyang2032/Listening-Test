#!/usr/bin/env python3
"""Process MUSHRA subjective listening test scores.

The default screening profile follows the practical post-screening rules in
ITU-R BS.1534-3 for MUSHRA-style tests:

* Convert raw subjective values linearly to the normalized 0-100 MUSHRA scale.
* Exclude an assessor when hidden-reference scores are lower than 90 for more
  than 15% of test items.
* Exclude an assessor when anchor scores are higher than 20 for more than 15%
  of test items.
* If more than 25% of assessors rate the anchor above 20 for the same item,
  the anchor may not be sufficiently degraded; anchor failures for that item
  are reported but not used to exclude assessors by default.

All thresholds are configurable from the command line or a JSON config file.
The script intentionally uses only the Python standard library and writes
SVG/PNG boxplots so it can run in lightweight lab environments.
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import math
import os
import struct
import re
import statistics
import sys
import tempfile
import zlib
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


DEFAULT_HIDDEN_REF_NAMES = (
    "hidden reference",
    "hidden_reference",
    "hidden-reference",
    "hiddenref",
    "hidden ref",
    "hr",
    "reference",
    "ref",
    "原始参考",
    "隐藏参考",
    "隐式参考",
    "参考",
)

DEFAULT_ANCHOR_NAMES = (
    "anchor",
    "low anchor",
    "low_anchor",
    "low-anchor",
    "mid anchor",
    "mid_anchor",
    "mid-anchor",
    "lowpass anchor",
    "low-pass anchor",
    "lp35",
    "lp3.5",
    "3.5khz",
    "3.5 khz",
    "7khz",
    "7 khz",
    "锚点",
    "低锚点",
    "中锚点",
    "锚定",
)

SUBJECT_ALIASES = (
    "subject",
    "subject_id",
    "listener",
    "listener_id",
    "participant",
    "participant_id",
    "assessor",
    "assessor_id",
    "rater",
    "user",
    "user_id",
    "听音者",
    "受试者",
    "被试",
    "评价者",
    "用户",
)

ITEM_ALIASES = (
    "item",
    "item_id",
    "trial",
    "trial_id",
    "program",
    "program_id",
    "sample",
    "sample_id",
    "stimulus",
    "stimulus_id",
    "content",
    "audio",
    "file",
    "material",
    "材料",
    "节目",
    "样本",
    "音频",
    "文件",
)

CONDITION_ALIASES = (
    "condition",
    "condition_id",
    "system",
    "system_id",
    "codec",
    "method",
    "model",
    "processing",
    "version",
    "算法",
    "系统",
    "条件",
    "处理",
    "版本",
)

SCORE_ALIASES = (
    "score",
    "rating",
    "grade",
    "value",
    "mushra",
    "points",
    "评分",
    "分数",
    "得分",
    "打分",
)

ROLE_ALIASES = (
    "role",
    "type",
    "control_role",
    "condition_type",
    "stimulus_type",
    "角色",
    "类型",
    "条件类型",
)

WIDE_METADATA_ALIASES = set(
    SUBJECT_ALIASES
    + ITEM_ALIASES
    + ROLE_ALIASES
    + (
        "session",
        "session_id",
        "date",
        "time",
        "order",
        "age",
        "gender",
        "comment",
        "comments",
        "备注",
        "日期",
        "时间",
        "顺序",
        "性别",
        "年龄",
    )
)


@dataclass
class Rules:
    min_score: float = 0.0
    max_score: float = 100.0
    raw_score_min: float = 0.0
    raw_score_max: float = 100.0
    hidden_ref_min: float = 90.0
    max_hidden_ref_fail_rate: float = 0.15
    anchor_max: float = 20.0
    max_anchor_fail_rate: float = 0.15
    anchor_item_waiver_rate: float | None = 0.25
    min_ref_anchor_delta: float | None = None
    max_ref_anchor_delta_fail_rate: float = 0.15
    screening_unit: str = "listener"
    missing_control_policy: str = "warn"
    remove_statistical_outliers: bool = False


def canonical_label(value: Any) -> str:
    text = "" if value is None else str(value)
    text = text.strip().casefold()
    return re.sub(r"[\s_\-./()（）\\]+", "", text)


def parse_float(value: Any) -> tuple[float | None, str]:
    if value is None:
        return None, "missing"
    if isinstance(value, (int, float)):
        number = float(value)
        if math.isfinite(number):
            return number, ""
        return None, "not finite"

    text = str(value).strip()
    if not text:
        return None, "missing"

    text = text.replace("\u00a0", "").replace(" ", "")
    if text.endswith("%"):
        text = text[:-1]
    if "," in text and "." not in text:
        text = text.replace(",", ".")
    else:
        text = text.replace(",", "")

    try:
        number = float(text)
    except ValueError:
        return None, f"not numeric: {value}"
    if not math.isfinite(number):
        return None, "not finite"
    return number, ""


def pct(value: float | None) -> str:
    if value is None:
        return ""
    return f"{value * 100:.2f}%"


def fmt(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return ""
        return f"{value:.6g}"
    return value


def median_of_ordered(values: list[float]) -> float:
    if not values:
        raise ValueError("median requires at least one value")
    midpoint = len(values) // 2
    if len(values) % 2:
        return values[midpoint]
    return (values[midpoint - 1] + values[midpoint]) / 2


def bs1534_quartiles(ordered: list[float]) -> tuple[float, float, float]:
    """Return median, Q1 and Q3 using the inclusive BS.1534 quartile rule."""
    median = median_of_ordered(ordered)
    midpoint = len(ordered) // 2
    if len(ordered) % 2:
        lower_half = ordered[: midpoint + 1]
        upper_half = ordered[midpoint:]
    else:
        lower_half = ordered[:midpoint]
        upper_half = ordered[midpoint:]
    q1 = median_of_ordered(lower_half)
    q3 = median_of_ordered(upper_half)
    return median, q1, q3


def box_stats(values: list[float]) -> dict[str, Any]:
    ordered = sorted(values)
    if not ordered:
        return {
            "n": 0,
            "q1": None,
            "median": None,
            "q3": None,
            "iqr": None,
            "lower_fence": None,
            "upper_fence": None,
            "whisker_low": None,
            "whisker_high": None,
            "outliers": [],
        }

    median, q1, q3 = bs1534_quartiles(ordered)
    iqr = q3 - q1
    lower_fence = q1 - 1.5 * iqr
    upper_fence = q3 + 1.5 * iqr
    non_outliers = [x for x in ordered if lower_fence <= x <= upper_fence]
    outliers = [x for x in ordered if x < lower_fence or x > upper_fence]
    return {
        "n": len(ordered),
        "q1": q1,
        "median": median,
        "q3": q3,
        "iqr": iqr,
        "lower_fence": lower_fence,
        "upper_fence": upper_fence,
        "whisker_low": min(non_outliers) if non_outliers else min(ordered),
        "whisker_high": max(non_outliers) if non_outliers else max(ordered),
        "outliers": outliers,
    }


def read_table(path: Path, sheet: str | None = None) -> tuple[list[dict[str, Any]], list[str]]:
    suffix = path.suffix.lower()
    if suffix in {".csv", ".tsv", ".txt"}:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            sample = handle.read(4096)
            handle.seek(0)
            if suffix == ".tsv":
                dialect = csv.excel_tab
            else:
                try:
                    dialect = csv.Sniffer().sniff(sample, delimiters=",\t;")
                except csv.Error:
                    dialect = csv.excel
            reader = csv.DictReader(handle, dialect=dialect)
            rows = [dict(row) for row in reader]
            return rows, list(reader.fieldnames or [])

    if suffix in {".jsonl", ".ndjson"}:
        rows = []
        headers: list[str] = []
        seen = set()
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                line = line.strip()
                if not line:
                    continue
                payload = json.loads(line)
                if not isinstance(payload, dict):
                    raise ValueError(f"JSONL line {line_number} is not an object")
                rows.append(payload)
                for key in payload:
                    if key not in seen:
                        headers.append(key)
                        seen.add(key)
        return rows, headers

    if suffix == ".json":
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, list) or not all(isinstance(row, dict) for row in payload):
            raise ValueError("JSON input must be a list of objects")
        headers = []
        seen = set()
        for row in payload:
            for key in row:
                if key not in seen:
                    headers.append(key)
                    seen.add(key)
        return payload, headers

    if suffix in {".xlsx", ".xlsm"}:
        try:
            import openpyxl  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "XLSX input requires openpyxl. Export the sheet as CSV, or install openpyxl."
            ) from exc

        workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
        worksheet = workbook[sheet] if sheet else workbook.active
        values = list(worksheet.iter_rows(values_only=True))
        if not values:
            return [], []
        headers = ["" if cell is None else str(cell) for cell in values[0]]
        rows = []
        for row in values[1:]:
            rows.append({headers[i]: row[i] if i < len(row) else "" for i in range(len(headers))})
        return rows, headers

    raise ValueError(f"Unsupported input format: {path.suffix}")


def resolve_column(
    headers: list[str],
    requested: str | None,
    aliases: Iterable[str],
    *,
    required: bool,
    label: str,
) -> str | None:
    if requested:
        if requested in headers:
            return requested
        normalized_requested = canonical_label(requested)
        for header in headers:
            if canonical_label(header) == normalized_requested:
                return header
        if required:
            raise ValueError(f"Column '{requested}' for {label} was not found")
        return None

    alias_set = {canonical_label(alias) for alias in aliases}
    for header in headers:
        if canonical_label(header) in alias_set:
            return header
    if required:
        raise ValueError(
            f"Could not infer {label} column. Use --{label.replace('_', '-')}-col."
        )
    return None


def infer_role(
    condition: Any,
    role_value: Any,
    hidden_ref_names: set[str],
    anchor_names: set[str],
) -> str:
    candidates = []
    if role_value not in (None, ""):
        candidates.append(canonical_label(role_value))
    candidates.append(canonical_label(condition))

    for label in candidates:
        if not label:
            continue
        if label in anchor_names or "anchor" in label or "锚" in label:
            return "anchor"
        if label in hidden_ref_names:
            return "hidden_reference"
        if "hidden" in label and ("ref" in label or "reference" in label):
            return "hidden_reference"
        if ("隐藏" in label or "隐式" in label) and "参考" in label:
            return "hidden_reference"
        if label in {"system", "sys", "sut", "test", "codec", "算法", "系统"}:
            return "system"

    return "system"


def normalize_score(raw_score: float, rules: Rules) -> float:
    if rules.raw_score_max <= rules.raw_score_min:
        raise ValueError("raw_score_max must be greater than raw_score_min")
    fraction = (raw_score - rules.raw_score_min) / (rules.raw_score_max - rules.raw_score_min)
    return rules.min_score + fraction * (rules.max_score - rules.min_score)


def score_issue(raw_score: float | None, parse_issue: str, rules: Rules) -> str:
    if parse_issue:
        return parse_issue
    if raw_score is None:
        return "missing"
    if raw_score < rules.raw_score_min or raw_score > rules.raw_score_max:
        return f"raw score outside [{rules.raw_score_min}, {rules.raw_score_max}]"
    return ""


def load_ratings(
    rows: list[dict[str, Any]],
    headers: list[str],
    args: argparse.Namespace,
    rules: Rules,
    warnings: list[str],
) -> tuple[list[dict[str, Any]], list[str], dict[str, str | None]]:
    hidden_ref_names = {canonical_label(x) for x in DEFAULT_HIDDEN_REF_NAMES}
    hidden_ref_names.update(canonical_label(x) for x in args.hidden_ref_condition or [])
    anchor_names = {canonical_label(x) for x in DEFAULT_ANCHOR_NAMES}
    anchor_names.update(canonical_label(x) for x in args.anchor_condition or [])

    subject_col = resolve_column(
        headers, args.subject_col, SUBJECT_ALIASES, required=True, label="subject"
    )
    item_col = resolve_column(headers, args.item_col, ITEM_ALIASES, required=False, label="item")
    role_col = resolve_column(headers, args.role_col, ROLE_ALIASES, required=False, label="role")
    if not item_col:
        warnings.append("未找到 item/trial/material 列，所有评分按同一个材料 '__single_item__' 处理。")

    condition_col = resolve_column(
        headers, args.condition_col, CONDITION_ALIASES, required=False, label="condition"
    )
    score_col = resolve_column(headers, args.score_col, SCORE_ALIASES, required=False, label="score")

    input_format = args.input_format
    if input_format == "auto":
        input_format = "long" if condition_col and score_col else "wide"

    ratings: list[dict[str, Any]] = []
    condition_order: list[str] = []
    seen_conditions = set()

    def remember_condition(condition: str) -> None:
        if condition not in seen_conditions:
            condition_order.append(condition)
            seen_conditions.add(condition)

    if input_format == "long":
        if not condition_col or not score_col:
            raise ValueError(
                "Long format requires subject/item/condition/score columns. "
                "Use --condition-col and --score-col if automatic inference fails."
            )
        for row_number, row in enumerate(rows, 2):
            subject = str(row.get(subject_col, "")).strip()
            item = str(row.get(item_col, "__single_item__")).strip() if item_col else "__single_item__"
            condition = str(row.get(condition_col, "")).strip()
            role_value = row.get(role_col, "") if role_col else ""
            role = infer_role(condition, role_value, hidden_ref_names, anchor_names)
            raw_score, parse_issue = parse_float(row.get(score_col))
            issue = score_issue(raw_score, parse_issue, rules)
            score = normalize_score(raw_score, rules) if raw_score is not None and not issue else None
            remember_condition(condition)
            ratings.append(
                {
                    "source_row": row_number,
                    "source_column": score_col,
                    "subject": subject,
                    "item": item,
                    "condition": condition,
                    "role": role,
                    "raw_score": raw_score,
                    "score": score,
                    "invalid_score": bool(issue),
                    "score_issue": issue,
                }
            )

    else:
        if args.wide_score_columns:
            score_columns = [part.strip() for part in args.wide_score_columns.split(",") if part.strip()]
            missing = [column for column in score_columns if column not in headers]
            if missing:
                raise ValueError(f"Wide score columns not found: {', '.join(missing)}")
        else:
            protected = {
                column
                for column in (subject_col, item_col, role_col)
                if column is not None
            }
            score_columns = []
            for header in headers:
                if header in protected:
                    continue
                if canonical_label(header) in {canonical_label(x) for x in WIDE_METADATA_ALIASES}:
                    continue
                numeric_count = 0
                for row in rows:
                    number, issue = parse_float(row.get(header))
                    if issue == "" and number is not None:
                        numeric_count += 1
                if numeric_count:
                    score_columns.append(header)
            if not score_columns:
                raise ValueError(
                    "Could not infer wide score columns. Use --wide-score-columns A,B,C."
                )
            warnings.append(
                "按宽表读取，自动识别评分列："
                + ", ".join(score_columns)
                + "。如识别不准确，请用 --wide-score-columns 指定。"
            )

        for row_number, row in enumerate(rows, 2):
            subject = str(row.get(subject_col, "")).strip()
            item = str(row.get(item_col, "__single_item__")).strip() if item_col else "__single_item__"
            role_value = row.get(role_col, "") if role_col else ""
            for column in score_columns:
                condition = column
                role = infer_role(condition, role_value, hidden_ref_names, anchor_names)
                raw_score, parse_issue = parse_float(row.get(column))
                issue = score_issue(raw_score, parse_issue, rules)
                score = normalize_score(raw_score, rules) if raw_score is not None and not issue else None
                remember_condition(condition)
                ratings.append(
                    {
                        "source_row": row_number,
                        "source_column": column,
                        "subject": subject,
                        "item": item,
                        "condition": condition,
                        "role": role,
                        "raw_score": raw_score,
                        "score": score,
                        "invalid_score": bool(issue),
                        "score_issue": issue,
                    }
                )

    column_info = {
        "subject_col": subject_col,
        "item_col": item_col,
        "condition_col": condition_col if input_format == "long" else None,
        "score_col": score_col if input_format == "long" else None,
        "role_col": role_col,
        "input_format": input_format,
    }
    return ratings, condition_order, column_info


def mean_or_none(values: list[float]) -> float | None:
    return statistics.fmean(values) if values else None


def screen_ratings(
    ratings: list[dict[str, Any]],
    rules: Rules,
    warnings: list[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    trials: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for rating in ratings:
        trials[(rating["subject"], rating["item"])].append(rating)

    trial_rows: list[dict[str, Any]] = []
    anchor_fail_by_item: dict[str, list[bool]] = defaultdict(list)

    for (subject, item), group in trials.items():
        hidden_scores = [
            rating["score"]
            for rating in group
            if rating["role"] == "hidden_reference"
            and not rating["invalid_score"]
            and rating["score"] is not None
        ]
        anchor_scores = [
            rating["score"]
            for rating in group
            if rating["role"] == "anchor"
            and not rating["invalid_score"]
            and rating["score"] is not None
        ]
        hidden_mean = mean_or_none(hidden_scores)
        anchor_max = max(anchor_scores) if anchor_scores else None
        anchor_mean = mean_or_none(anchor_scores)
        anchor_fail = anchor_max is not None and anchor_max > rules.anchor_max
        if anchor_max is not None:
            anchor_fail_by_item[item].append(anchor_fail)

        trial_rows.append(
            {
                "subject": subject,
                "item": item,
                "rating_count": len(group),
                "hidden_ref_score": hidden_mean,
                "anchor_score_max": anchor_max,
                "anchor_score_mean": anchor_mean,
                "missing_hidden_ref": hidden_mean is None,
                "missing_anchor": anchor_max is None,
                "hidden_ref_fail": hidden_mean is not None and hidden_mean < rules.hidden_ref_min,
                "anchor_fail_raw": anchor_fail,
                "anchor_fail_waived": False,
                "anchor_fail": anchor_fail,
                "ref_anchor_delta": (
                    hidden_mean - anchor_max
                    if hidden_mean is not None and anchor_max is not None
                    else None
                ),
                "ref_anchor_delta_fail": False,
                "trial_fail": False,
                "trial_fail_reasons": "",
            }
        )

    waived_anchor_items: set[str] = set()
    if rules.anchor_item_waiver_rate is not None:
        for item, flags in anchor_fail_by_item.items():
            if flags and (sum(flags) / len(flags)) > rules.anchor_item_waiver_rate:
                waived_anchor_items.add(item)
        if waived_anchor_items:
            warnings.append(
                "以下材料超过 "
                f"{pct(rules.anchor_item_waiver_rate)} 的听音者给 anchor 打分高于 "
                f"{rules.anchor_max:g}，按 BS.1534-3 建议不把这些 anchor 失败计入听音者剔除："
                + ", ".join(sorted(waived_anchor_items))
            )

    missing_hidden_count = 0
    missing_anchor_count = 0
    for row in trial_rows:
        reasons = []
        if row["missing_hidden_ref"]:
            missing_hidden_count += 1
            if rules.missing_control_policy == "fail":
                row["hidden_ref_fail"] = True
                reasons.append("missing hidden reference")
            elif rules.missing_control_policy == "warn":
                reasons.append("missing hidden reference (not screened)")
        if row["missing_anchor"]:
            missing_anchor_count += 1
            if rules.missing_control_policy == "fail":
                row["anchor_fail"] = True
                reasons.append("missing anchor")
            elif rules.missing_control_policy == "warn":
                reasons.append("missing anchor (not screened)")

        if row["item"] in waived_anchor_items and row["anchor_fail_raw"]:
            row["anchor_fail_waived"] = True
            row["anchor_fail"] = False

        if row["hidden_ref_fail"]:
            reasons.append(
                f"hidden reference < {rules.hidden_ref_min:g}"
                if not row["missing_hidden_ref"]
                else "hidden reference failure"
            )
        if row["anchor_fail"]:
            reasons.append(
                f"anchor > {rules.anchor_max:g}"
                if not row["missing_anchor"]
                else "anchor failure"
            )
        if row["anchor_fail_waived"]:
            reasons.append(f"anchor > {rules.anchor_max:g} (item-level waiver)")

        if rules.min_ref_anchor_delta is not None and row["ref_anchor_delta"] is not None:
            if row["ref_anchor_delta"] < rules.min_ref_anchor_delta:
                row["ref_anchor_delta_fail"] = True
                reasons.append(f"hidden_ref - anchor < {rules.min_ref_anchor_delta:g}")

        row["trial_fail"] = bool(
            row["hidden_ref_fail"] or row["anchor_fail"] or row["ref_anchor_delta_fail"]
        )
        row["trial_fail_reasons"] = "; ".join(dict.fromkeys(reasons))

    if missing_hidden_count and rules.missing_control_policy == "warn":
        warnings.append(
            f"{missing_hidden_count} 个 subject-item 组合缺少 hidden reference，默认只告警不剔除。"
        )
    if missing_anchor_count and rules.missing_control_policy == "warn":
        warnings.append(f"{missing_anchor_count} 个 subject-item 组合缺少 anchor，默认只告警不剔除。")

    by_subject: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in trial_rows:
        by_subject[row["subject"]].append(row)

    listener_rows: list[dict[str, Any]] = []
    listener_exclusions: dict[str, str] = {}
    for subject, rows in sorted(by_subject.items()):
        hidden_trials = [
            row
            for row in rows
            if not row["missing_hidden_ref"] or rules.missing_control_policy == "fail"
        ]
        anchor_trials = [
            row for row in rows if not row["missing_anchor"] or rules.missing_control_policy == "fail"
        ]
        delta_trials = [row for row in rows if row["ref_anchor_delta"] is not None]
        if rules.missing_control_policy == "fail":
            delta_trials = rows

        hidden_fail_n = sum(1 for row in hidden_trials if row["hidden_ref_fail"])
        anchor_fail_n = sum(1 for row in anchor_trials if row["anchor_fail"])
        delta_fail_n = sum(1 for row in delta_trials if row["ref_anchor_delta_fail"])

        hidden_rate = hidden_fail_n / len(hidden_trials) if hidden_trials else None
        anchor_rate = anchor_fail_n / len(anchor_trials) if anchor_trials else None
        delta_rate = delta_fail_n / len(delta_trials) if delta_trials else None

        reasons = []
        if hidden_rate is not None and hidden_rate > rules.max_hidden_ref_fail_rate:
            reasons.append(
                f"hidden_ref_fail_rate {pct(hidden_rate)} > {pct(rules.max_hidden_ref_fail_rate)}"
            )
        if anchor_rate is not None and anchor_rate > rules.max_anchor_fail_rate:
            reasons.append(f"anchor_fail_rate {pct(anchor_rate)} > {pct(rules.max_anchor_fail_rate)}")
        if (
            rules.min_ref_anchor_delta is not None
            and delta_rate is not None
            and delta_rate > rules.max_ref_anchor_delta_fail_rate
        ):
            reasons.append(
                f"ref_anchor_delta_fail_rate {pct(delta_rate)} > "
                f"{pct(rules.max_ref_anchor_delta_fail_rate)}"
            )

        excluded = bool(reasons) and rules.screening_unit in {"listener", "both"}
        if excluded:
            listener_exclusions[subject] = "; ".join(reasons)

        listener_rows.append(
            {
                "subject": subject,
                "trial_count": len(rows),
                "rating_count": sum(row["rating_count"] for row in rows),
                "hidden_ref_trials": len(hidden_trials),
                "hidden_ref_fail_n": hidden_fail_n,
                "hidden_ref_fail_rate": hidden_rate,
                "anchor_trials": len(anchor_trials),
                "anchor_fail_n": anchor_fail_n,
                "anchor_fail_rate": anchor_rate,
                "ref_anchor_delta_trials": len(delta_trials),
                "ref_anchor_delta_fail_n": delta_fail_n,
                "ref_anchor_delta_fail_rate": delta_rate,
                "excluded": excluded,
                "exclusion_reason": "; ".join(reasons),
            }
        )

    trial_exclusions: dict[tuple[str, str], str] = {}
    if rules.screening_unit in {"trial", "both"}:
        for row in trial_rows:
            if row["trial_fail"] and row["subject"] not in listener_exclusions:
                trial_exclusions[(row["subject"], row["item"])] = row["trial_fail_reasons"]

    for rating in ratings:
        reasons = []
        if rating["invalid_score"]:
            reasons.append(f"invalid score: {rating['score_issue']}")
        subject_reason = listener_exclusions.get(rating["subject"])
        if subject_reason:
            reasons.append(f"listener excluded: {subject_reason}")
        trial_reason = trial_exclusions.get((rating["subject"], rating["item"]))
        if trial_reason:
            reasons.append(f"trial excluded: {trial_reason}")
        rating["listener_excluded"] = bool(subject_reason)
        rating["trial_excluded"] = bool(trial_reason)
        rating["retained"] = not reasons
        rating["exclusion_reason"] = "; ".join(reasons)

    return listener_rows, trial_rows, sorted(waived_anchor_items)


def summarize_conditions(
    ratings: list[dict[str, Any]],
    condition_order: list[str],
    *,
    include_controls: bool,
) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for rating in ratings:
        if not rating["retained"]:
            continue
        if not include_controls and rating["role"] != "system":
            continue
        groups[rating["condition"]].append(rating)

    order_index = {condition: i for i, condition in enumerate(condition_order)}
    summaries: list[dict[str, Any]] = []
    for condition in sorted(groups, key=lambda name: order_index.get(name, 10_000)):
        group = groups[condition]
        scores = [rating["score"] for rating in group if rating["score"] is not None]
        scores = [float(score) for score in scores]
        stats = box_stats(scores)
        role = Counter(rating["role"] for rating in group).most_common(1)[0][0]
        n = len(scores)
        mean = statistics.fmean(scores) if scores else None
        std = statistics.stdev(scores) if len(scores) > 1 else None
        se = std / math.sqrt(n) if std is not None and n else None
        ci95 = 1.96 * se if se is not None else None
        summaries.append(
            {
                "condition": condition,
                "role": role,
                "n": n,
                "subject_count": len({rating["subject"] for rating in group}),
                "item_count": len({rating["item"] for rating in group}),
                "mean": mean,
                "median": stats["median"],
                "std": std,
                "se": se,
                "ci95_low": mean - ci95 if mean is not None and ci95 is not None else None,
                "ci95_high": mean + ci95 if mean is not None and ci95 is not None else None,
                "min": min(scores) if scores else None,
                "q1": stats["q1"],
                "q3": stats["q3"],
                "max": max(scores) if scores else None,
                "iqr": stats["iqr"],
                "whisker_low": stats["whisker_low"],
                "whisker_high": stats["whisker_high"],
                "outlier_count": len(stats["outliers"]),
                "outlier_values": ";".join(fmt(value) for value in stats["outliers"]),
            }
        )

    ranked_systems = sorted(
        [row for row in summaries if row["role"] == "system" and row["mean"] is not None],
        key=lambda row: row["mean"],
        reverse=True,
    )
    rank_by_condition = {row["condition"]: i + 1 for i, row in enumerate(ranked_systems)}
    for row in summaries:
        row["system_rank_by_mean"] = rank_by_condition.get(row["condition"], "")
    return summaries


def find_statistical_outliers(ratings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for rating in ratings:
        if rating["retained"] and rating["score"] is not None:
            groups[(rating["condition"], rating["item"])].append(rating)

    outliers: list[dict[str, Any]] = []
    for (condition, item), group in groups.items():
        stats = box_stats([float(rating["score"]) for rating in group])
        lower = stats["lower_fence"]
        upper = stats["upper_fence"]
        if lower is None or upper is None:
            continue
        for rating in group:
            score = float(rating["score"])
            if score < lower or score > upper:
                outliers.append(
                    {
                        "subject": rating["subject"],
                        "item": item,
                        "condition": condition,
                        "role": rating["role"],
                        "score": score,
                        "lower_fence": lower,
                        "upper_fence": upper,
                        "source_row": rating["source_row"],
                        "source_column": rating["source_column"],
                    }
                )
    return outliers


def remove_outliers(ratings: list[dict[str, Any]], outliers: list[dict[str, Any]]) -> None:
    outlier_keys = {
        (
            row["source_row"],
            row["source_column"],
            row["subject"],
            row["item"],
            row["condition"],
            row["score"],
        )
        for row in outliers
    }
    for rating in ratings:
        key = (
            rating["source_row"],
            rating["source_column"],
            rating["subject"],
            rating["item"],
            rating["condition"],
            rating["score"],
        )
        if key in outlier_keys:
            rating["retained"] = False
            reason = "statistical outlier by 1.5*IQR"
            rating["exclusion_reason"] = (
                f"{rating['exclusion_reason']}; {reason}" if rating["exclusion_reason"] else reason
            )


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: fmt(row.get(field)) for field in fieldnames})


def svg_boxplot(
    path: Path,
    summaries: list[dict[str, Any]],
    ratings: list[dict[str, Any]],
    rules: Rules,
    *,
    include_controls: bool,
) -> None:
    plot_summaries = [
        row
        for row in summaries
        if row["n"] and (include_controls or row["role"] == "system")
    ]
    if not plot_summaries:
        path.write_text(
            '<svg xmlns="http://www.w3.org/2000/svg" width="900" height="360">'
            '<text x="30" y="40">No retained data to plot.</text></svg>',
            encoding="utf-8",
        )
        clear_quarantine(path)
        return

    condition_to_scores: dict[str, list[float]] = defaultdict(list)
    for rating in ratings:
        if not rating["retained"] or rating["score"] is None:
            continue
        if not include_controls and rating["role"] != "system":
            continue
        condition_to_scores[rating["condition"]].append(float(rating["score"]))

    width = max(900, 140 + 90 * len(plot_summaries))
    height = 560
    left = 70
    right = 30
    top = 50
    bottom = 145
    plot_width = width - left - right
    plot_height = height - top - bottom
    ymin = rules.min_score
    ymax = rules.max_score
    if ymax <= ymin:
        ymin, ymax = 0.0, 100.0

    def y(score: float) -> float:
        return top + (ymax - score) / (ymax - ymin) * plot_height

    step = plot_width / max(len(plot_summaries), 1)
    box_width = min(42, step * 0.48)
    palette = {
        "system": ("#3b82f6", "#1d4ed8"),
        "anchor": ("#f97316", "#c2410c"),
        "hidden_reference": ("#10b981", "#047857"),
    }

    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">',
        "<style>",
        "text{font-family:'STHeiti','SimHei','Heiti SC','Microsoft YaHei',sans-serif;fill:#111827}",
        ".axis{stroke:#374151;stroke-width:1}",
        ".grid{stroke:#e5e7eb;stroke-width:1}",
        ".label{font-size:12px}",
        ".small{font-size:11px;fill:#4b5563}",
        "</style>",
        f'<rect x="0" y="0" width="{width}" height="{height}" fill="#ffffff"/>',
        f'<text x="{left}" y="28" font-size="18" font-weight="700">MUSHRA boxplot with outliers</text>',
    ]

    tick_count = 5
    for tick in range(tick_count + 1):
        value = ymin + (ymax - ymin) * tick / tick_count
        yy = y(value)
        lines.append(
            f'<line class="grid" x1="{left}" y1="{yy:.2f}" x2="{width-right}" y2="{yy:.2f}"/>'
        )
        lines.append(
            f'<text class="small" x="{left-10}" y="{yy+4:.2f}" text-anchor="end">{value:g}</text>'
        )
    lines.append(f'<line class="axis" x1="{left}" y1="{top}" x2="{left}" y2="{height-bottom}"/>')
    lines.append(
        f'<line class="axis" x1="{left}" y1="{height-bottom}" x2="{width-right}" y2="{height-bottom}"/>'
    )

    for index, summary in enumerate(plot_summaries):
        condition = summary["condition"]
        role = summary["role"]
        scores = condition_to_scores[condition]
        stats = box_stats(scores)
        cx = left + step * index + step / 2
        fill, stroke = palette.get(role, ("#94a3b8", "#475569"))
        q1 = stats["q1"]
        q3 = stats["q3"]
        median = stats["median"]
        whisker_low = stats["whisker_low"]
        whisker_high = stats["whisker_high"]
        if None in {q1, q3, median, whisker_low, whisker_high}:
            continue

        y_q1 = y(float(q1))
        y_q3 = y(float(q3))
        y_med = y(float(median))
        y_low = y(float(whisker_low))
        y_high = y(float(whisker_high))
        box_top = min(y_q1, y_q3)
        box_height = max(1.0, abs(y_q1 - y_q3))

        lines.append(
            f'<line x1="{cx:.2f}" y1="{y_high:.2f}" x2="{cx:.2f}" y2="{box_top:.2f}" '
            f'stroke="{stroke}" stroke-width="1.5"/>'
        )
        lines.append(
            f'<line x1="{cx:.2f}" y1="{box_top+box_height:.2f}" x2="{cx:.2f}" y2="{y_low:.2f}" '
            f'stroke="{stroke}" stroke-width="1.5"/>'
        )
        lines.append(
            f'<line x1="{cx-box_width/3:.2f}" y1="{y_high:.2f}" x2="{cx+box_width/3:.2f}" '
            f'y2="{y_high:.2f}" stroke="{stroke}" stroke-width="1.5"/>'
        )
        lines.append(
            f'<line x1="{cx-box_width/3:.2f}" y1="{y_low:.2f}" x2="{cx+box_width/3:.2f}" '
            f'y2="{y_low:.2f}" stroke="{stroke}" stroke-width="1.5"/>'
        )
        lines.append(
            f'<rect x="{cx-box_width/2:.2f}" y="{box_top:.2f}" width="{box_width:.2f}" '
            f'height="{box_height:.2f}" fill="{fill}" fill-opacity="0.35" '
            f'stroke="{stroke}" stroke-width="1.5"/>'
        )
        lines.append(
            f'<line x1="{cx-box_width/2:.2f}" y1="{y_med:.2f}" x2="{cx+box_width/2:.2f}" '
            f'y2="{y_med:.2f}" stroke="{stroke}" stroke-width="2.2"/>'
        )

        for outlier_index, outlier in enumerate(stats["outliers"]):
            jitter = ((outlier_index % 5) - 2) * 3.2
            lines.append(
                f'<circle cx="{cx+jitter:.2f}" cy="{y(float(outlier)):.2f}" r="3.2" '
                f'fill="#111827" fill-opacity="0.82">'
                f'<title>{html.escape(condition)} outlier: {fmt(outlier)}</title></circle>'
            )

        safe_condition = html.escape(condition)
        label = safe_condition if len(safe_condition) <= 24 else safe_condition[:21] + "..."
        lines.append(
            f'<text class="label" x="{cx:.2f}" y="{height-bottom+18}" text-anchor="end" '
            f'transform="rotate(-35 {cx:.2f} {height-bottom+18})">{label}</text>'
        )
        lines.append(
            f'<text class="small" x="{cx:.2f}" y="{height-bottom+34}" text-anchor="middle">n={summary["n"]}</text>'
        )

    legend_y = 30
    legend_x = width - 360
    for offset, (role, label) in enumerate(
        (
            ("hidden_reference", "Hidden Reference"),
            ("anchor", "Anchor"),
            ("system", "System"),
        )
    ):
        fill, stroke = palette[role]
        x = legend_x + offset * 125
        lines.append(
            f'<rect x="{x}" y="{legend_y-12}" width="14" height="14" fill="{fill}" '
            f'fill-opacity="0.35" stroke="{stroke}"/>'
        )
        lines.append(f'<text class="small" x="{x+20}" y="{legend_y}">{label}</text>')

    lines.append("</svg>")
    path.write_text("\n".join(lines), encoding="utf-8")
    clear_quarantine(path)


def png_chunk(kind: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + kind
        + data
        + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    )


def write_png(path: Path, width: int, height: int, rgba: bytearray) -> None:
    raw = bytearray()
    stride = width * 4
    for y_pos in range(height):
        raw.append(0)
        start = y_pos * stride
        raw.extend(rgba[start : start + stride])
    payload = (
        b"\x89PNG\r\n\x1a\n"
        + png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
        + png_chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + png_chunk(b"IEND", b"")
    )
    path.write_bytes(payload)
    clear_quarantine(path)


def clear_quarantine(path: Path) -> None:
    try:
        os.removexattr(path, "com.apple.quarantine")
    except (AttributeError, FileNotFoundError, OSError):
        pass


def set_pixel(canvas: bytearray, width: int, height: int, x_pos: int, y_pos: int, color: tuple[int, int, int, int]) -> None:
    if 0 <= x_pos < width and 0 <= y_pos < height:
        index = (y_pos * width + x_pos) * 4
        canvas[index : index + 4] = bytes(color)


def draw_line(
    canvas: bytearray,
    width: int,
    height: int,
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    color: tuple[int, int, int, int],
    thickness: int = 1,
) -> None:
    x1_i, y1_i, x2_i, y2_i = round(x1), round(y1), round(x2), round(y2)
    dx = abs(x2_i - x1_i)
    sx = 1 if x1_i < x2_i else -1
    dy = -abs(y2_i - y1_i)
    sy = 1 if y1_i < y2_i else -1
    error = dx + dy
    half = max(0, thickness // 2)
    while True:
        for ox in range(-half, half + 1):
            for oy in range(-half, half + 1):
                set_pixel(canvas, width, height, x1_i + ox, y1_i + oy, color)
        if x1_i == x2_i and y1_i == y2_i:
            break
        e2 = 2 * error
        if e2 >= dy:
            error += dy
            x1_i += sx
        if e2 <= dx:
            error += dx
            y1_i += sy


def draw_rect(
    canvas: bytearray,
    width: int,
    height: int,
    x_pos: float,
    y_pos: float,
    rect_width: float,
    rect_height: float,
    fill: tuple[int, int, int, int],
    stroke: tuple[int, int, int, int],
) -> None:
    left = round(x_pos)
    top = round(y_pos)
    right = round(x_pos + rect_width)
    bottom = round(y_pos + rect_height)
    for yy in range(top, bottom + 1):
        for xx in range(left, right + 1):
            if 0 <= xx < width and 0 <= yy < height:
                if xx in {left, right} or yy in {top, bottom}:
                    set_pixel(canvas, width, height, xx, yy, stroke)
                else:
                    alpha = fill[3] / 255
                    index = (yy * width + xx) * 4
                    old = canvas[index : index + 4]
                    blended = tuple(round(fill[i] * alpha + old[i] * (1 - alpha)) for i in range(3))
                    canvas[index : index + 4] = bytes((*blended, 255))


def draw_circle(
    canvas: bytearray,
    width: int,
    height: int,
    cx: float,
    cy: float,
    radius: float,
    color: tuple[int, int, int, int],
) -> None:
    cx_i = round(cx)
    cy_i = round(cy)
    r_i = round(radius)
    for yy in range(cy_i - r_i, cy_i + r_i + 1):
        for xx in range(cx_i - r_i, cx_i + r_i + 1):
            if (xx - cx_i) ** 2 + (yy - cy_i) ** 2 <= r_i**2:
                set_pixel(canvas, width, height, xx, yy, color)


FONT_3X5 = {
    "0": ("111", "101", "101", "101", "111"),
    "1": ("010", "110", "010", "010", "111"),
    "2": ("111", "001", "111", "100", "111"),
    "3": ("111", "001", "111", "001", "111"),
    "4": ("101", "101", "111", "001", "001"),
    "5": ("111", "100", "111", "001", "111"),
    "6": ("111", "100", "111", "101", "111"),
    "7": ("111", "001", "010", "010", "010"),
    "8": ("111", "101", "111", "101", "111"),
    "9": ("111", "101", "111", "001", "111"),
    "A": ("111", "101", "111", "101", "101"),
    "B": ("110", "101", "110", "101", "110"),
    "C": ("111", "100", "100", "100", "111"),
    "D": ("110", "101", "101", "101", "110"),
    "E": ("111", "100", "110", "100", "111"),
    "F": ("111", "100", "110", "100", "100"),
    "G": ("111", "100", "101", "101", "111"),
    "H": ("101", "101", "111", "101", "101"),
    "I": ("111", "010", "010", "010", "111"),
    "J": ("001", "001", "001", "101", "111"),
    "K": ("101", "101", "110", "101", "101"),
    "L": ("100", "100", "100", "100", "111"),
    "M": ("101", "111", "111", "101", "101"),
    "N": ("101", "111", "111", "111", "101"),
    "O": ("111", "101", "101", "101", "111"),
    "P": ("111", "101", "111", "100", "100"),
    "Q": ("111", "101", "101", "111", "001"),
    "R": ("111", "101", "111", "110", "101"),
    "S": ("111", "100", "111", "001", "111"),
    "T": ("111", "010", "010", "010", "010"),
    "U": ("101", "101", "101", "101", "111"),
    "V": ("101", "101", "101", "101", "010"),
    "W": ("101", "101", "111", "111", "101"),
    "X": ("101", "101", "010", "101", "101"),
    "Y": ("101", "101", "010", "010", "010"),
    "Z": ("111", "001", "010", "100", "111"),
    "-": ("000", "000", "111", "000", "000"),
    ".": ("000", "000", "000", "000", "010"),
    ":": ("000", "010", "000", "010", "000"),
    " ": ("000", "000", "000", "000", "000"),
}


def draw_text(
    canvas: bytearray,
    width: int,
    height: int,
    x_pos: int,
    y_pos: int,
    text: str,
    color: tuple[int, int, int, int],
    scale: int = 2,
) -> None:
    cursor = x_pos
    for char in text.upper():
        glyph = FONT_3X5.get(char, FONT_3X5[" "])
        for row_index, row in enumerate(glyph):
            for col_index, bit in enumerate(row):
                if bit == "1":
                    for sy in range(scale):
                        for sx in range(scale):
                            set_pixel(
                                canvas,
                                width,
                                height,
                                cursor + col_index * scale + sx,
                                y_pos + row_index * scale + sy,
                                color,
                            )
        cursor += 4 * scale


def png_boxplot(
    path: Path,
    summaries: list[dict[str, Any]],
    ratings: list[dict[str, Any]],
    rules: Rules,
    *,
    include_controls: bool,
) -> None:
    plot_summaries = [
        row for row in summaries if row["n"] and (include_controls or row["role"] == "system")
    ]
    width = max(900, 140 + 110 * len(plot_summaries))
    height = 560
    canvas = bytearray([255, 255, 255, 255] * width * height)
    if not plot_summaries:
        write_png(path, width, height, canvas)
        return

    condition_to_scores: dict[str, list[float]] = defaultdict(list)
    for rating in ratings:
        if not rating["retained"] or rating["score"] is None:
            continue
        if not include_controls and rating["role"] != "system":
            continue
        condition_to_scores[rating["condition"]].append(float(rating["score"]))

    left, right, top, bottom = 70, 30, 60, 130
    plot_width = width - left - right
    plot_height = height - top - bottom
    ymin, ymax = rules.min_score, rules.max_score

    def y(score: float) -> float:
        return top + (ymax - score) / (ymax - ymin) * plot_height

    grid = (229, 231, 235, 255)
    axis = (55, 65, 81, 255)
    text = (17, 24, 39, 255)
    palette = {
        "system": ((59, 130, 246, 90), (29, 78, 216, 255)),
        "anchor": ((249, 115, 22, 90), (194, 65, 12, 255)),
        "hidden_reference": ((16, 185, 129, 90), (4, 120, 87, 255)),
    }

    draw_text(canvas, width, height, left, 24, "MUSHRA BOXPLOT WITH OUTLIERS", text, scale=2)
    for tick in range(6):
        value = ymin + (ymax - ymin) * tick / 5
        yy = y(value)
        draw_line(canvas, width, height, left, yy, width - right, yy, grid)
        draw_text(canvas, width, height, 20, round(yy) - 5, f"{value:g}", text, scale=2)
    draw_line(canvas, width, height, left, top, left, height - bottom, axis, thickness=2)
    draw_line(canvas, width, height, left, height - bottom, width - right, height - bottom, axis, thickness=2)

    step = plot_width / max(len(plot_summaries), 1)
    box_width = min(46, step * 0.48)
    for index, summary in enumerate(plot_summaries):
        scores = condition_to_scores[summary["condition"]]
        stats = box_stats(scores)
        role = summary["role"]
        fill, stroke = palette.get(role, ((148, 163, 184, 90), (71, 85, 105, 255)))
        cx = left + step * index + step / 2
        q1, q3 = stats["q1"], stats["q3"]
        median = stats["median"]
        whisker_low, whisker_high = stats["whisker_low"], stats["whisker_high"]
        if None in {q1, q3, median, whisker_low, whisker_high}:
            continue
        y_q1, y_q3 = y(float(q1)), y(float(q3))
        y_med = y(float(median))
        y_low, y_high = y(float(whisker_low)), y(float(whisker_high))
        box_top = min(y_q1, y_q3)
        box_height = max(1.0, abs(y_q1 - y_q3))
        draw_line(canvas, width, height, cx, y_high, cx, box_top, stroke, thickness=2)
        draw_line(canvas, width, height, cx, box_top + box_height, cx, y_low, stroke, thickness=2)
        draw_line(canvas, width, height, cx - box_width / 3, y_high, cx + box_width / 3, y_high, stroke, thickness=2)
        draw_line(canvas, width, height, cx - box_width / 3, y_low, cx + box_width / 3, y_low, stroke, thickness=2)
        draw_rect(canvas, width, height, cx - box_width / 2, box_top, box_width, box_height, fill, stroke)
        draw_line(canvas, width, height, cx - box_width / 2, y_med, cx + box_width / 2, y_med, stroke, thickness=3)
        for outlier_index, outlier in enumerate(stats["outliers"]):
            jitter = ((outlier_index % 5) - 2) * 4
            draw_circle(canvas, width, height, cx + jitter, y(float(outlier)), 4, text)
        label = str(summary["condition"])[:12]
        draw_text(canvas, width, height, round(cx - len(label) * 4), height - bottom + 20, label, text, scale=2)
        draw_text(canvas, width, height, round(cx - 12), height - bottom + 42, f"N={summary['n']}", text, scale=2)

    write_png(path, width, height, canvas)


def write_conclusion(
    path: Path,
    ratings: list[dict[str, Any]],
    listener_rows: list[dict[str, Any]],
    trial_rows: list[dict[str, Any]],
    summaries_all: list[dict[str, Any]],
    summaries_systems: list[dict[str, Any]],
    outliers: list[dict[str, Any]],
    warnings: list[str],
    rules: Rules,
    column_info: dict[str, str | None],
    output_files: dict[str, Path],
) -> None:
    subjects_before = {rating["subject"] for rating in ratings}
    subjects_after = {rating["subject"] for rating in ratings if rating["retained"]}
    items_before = {rating["item"] for rating in ratings}
    items_after = {rating["item"] for rating in ratings if rating["retained"]}
    total = len(ratings)
    retained = sum(1 for rating in ratings if rating["retained"])
    invalid = sum(1 for rating in ratings if rating["invalid_score"])
    listener_excluded = [row for row in listener_rows if row["excluded"]]
    trial_excluded = {
        (rating["subject"], rating["item"])
        for rating in ratings
        if rating.get("trial_excluded")
    }
    system_ranked = [
        row
        for row in sorted(
            summaries_systems,
            key=lambda row: row["mean"] if row["mean"] is not None else -math.inf,
            reverse=True,
        )
        if row["mean"] is not None
    ]

    lines = [
        "# MUSHRA 主观评价数据处理结论",
        "",
        f"生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "## 采用的默认/有效规则",
        "",
        (
            f"- 原始评分按 ITU-R BS.1534-3 的线性换算归一化："
            f"[{rules.raw_score_min:g}, {rules.raw_score_max:g}] -> "
            f"[{rules.min_score:g}, {rules.max_score:g}]；"
            f"Hidden Reference 低于 {rules.hidden_ref_min:g} 记为失败；"
            f"Anchor 高于 {rules.anchor_max:g} 记为失败。"
        ),
        (
            f"- 听音者剔除阈值：Hidden Reference 失败率 > "
            f"{pct(rules.max_hidden_ref_fail_rate)}，Anchor 失败率 > "
            f"{pct(rules.max_anchor_fail_rate)}。"
        ),
        (
            "- Anchor 材料豁免："
            + (
                f"同一材料中超过 {pct(rules.anchor_item_waiver_rate)} 的听音者给 Anchor "
                f"高于 {rules.anchor_max:g} 时，该材料的 Anchor 失败不计入听音者剔除。"
                if rules.anchor_item_waiver_rate is not None
                else "已关闭。"
            )
        ),
        (
            "- Hidden Reference 与 Anchor 差值规则："
            + (
                f"HiddenRef-Anchor < {rules.min_ref_anchor_delta:g} 记为失败。"
                if rules.min_ref_anchor_delta is not None
                else "未启用。"
            )
        ),
        f"- 筛选粒度：{rules.screening_unit}；缺失控制项处理：{rules.missing_control_policy}。",
        "",
        "## 数据概况",
        "",
        f"- 输入格式识别：{column_info.get('input_format')}。",
        f"- 原始评分行数：{total}；有效保留评分行数：{retained}。",
        f"- 原始听音者数：{len(subjects_before)}；保留听音者数：{len(subjects_after)}。",
        f"- 原始材料数：{len(items_before)}；保留材料数：{len(items_after)}。",
        f"- 非数值/越界评分行数：{invalid}。",
        f"- 剔除听音者数：{len(listener_excluded)}；剔除 subject-item 组合数：{len(trial_excluded)}。",
        f"- 统计箱线图异常值数量：{len(outliers)}。",
        "",
    ]

    if listener_excluded:
        lines.extend(["## 被剔除听音者", ""])
        for row in listener_excluded:
            lines.append(f"- {row['subject']}：{row['exclusion_reason']}")
        lines.append("")

    if warnings:
        lines.extend(["## 处理告警", ""])
        for warning in warnings:
            lines.append(f"- {warning}")
        lines.append("")

    lines.extend(["## 系统条件结论", ""])
    if not system_ranked:
        lines.append("- 没有可用于系统条件统计的保留评分。")
    else:
        best = system_ranked[0]
        lines.append(
            f"- 按均值排序，当前最高的系统条件是 {best['condition']}，"
            f"均值 {fmt(best['mean'])}，中位数 {fmt(best['median'])}，n={best['n']}。"
        )
        if len(system_ranked) > 1:
            second = system_ranked[1]
            lines.append(
                f"- 第二名是 {second['condition']}，均值 {fmt(second['mean'])}；"
                f"与第一名均值差约 {fmt(float(best['mean']) - float(second['mean']))}。"
            )
        lines.append("- 完整均值、四分位数、95% 近似置信区间和异常值数量见 condition_summary*.csv。")
    lines.append("")

    lines.extend(["## 输出文件", ""])
    for label, file_path in output_files.items():
        lines.append(f"- {label}: `{file_path.name}`")
    lines.append("")
    lines.append(
        "说明：箱线图异常值按 1.5×IQR 标记并保留在图中；除非启用 "
        "`--remove-statistical-outliers`，这些统计异常值不会被自动剔除。"
    )

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_config(path: str | None) -> dict[str, Any]:
    if not path:
        return {}
    config = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise ValueError("Config file must contain a JSON object")
    return {key.replace("-", "_"): value for key, value in config.items()}


def get_value(args: argparse.Namespace, config: dict[str, Any], key: str, default: Any) -> Any:
    value = getattr(args, key)
    if value is not None:
        return value
    return config.get(key, default)


def as_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)]


def build_rules(args: argparse.Namespace, config: dict[str, Any]) -> Rules:
    waiver = get_value(args, config, "anchor_item_waiver_rate", Rules.anchor_item_waiver_rate)
    if args.disable_anchor_item_waiver:
        waiver = None
    return Rules(
        min_score=float(get_value(args, config, "min_score", Rules.min_score)),
        max_score=float(get_value(args, config, "max_score", Rules.max_score)),
        raw_score_min=float(get_value(args, config, "raw_score_min", Rules.raw_score_min)),
        raw_score_max=float(get_value(args, config, "raw_score_max", Rules.raw_score_max)),
        hidden_ref_min=float(get_value(args, config, "hidden_ref_min", Rules.hidden_ref_min)),
        max_hidden_ref_fail_rate=float(
            get_value(args, config, "max_hidden_ref_fail_rate", Rules.max_hidden_ref_fail_rate)
        ),
        anchor_max=float(get_value(args, config, "anchor_max", Rules.anchor_max)),
        max_anchor_fail_rate=float(
            get_value(args, config, "max_anchor_fail_rate", Rules.max_anchor_fail_rate)
        ),
        anchor_item_waiver_rate=None if waiver is None else float(waiver),
        min_ref_anchor_delta=(
            None
            if get_value(args, config, "min_ref_anchor_delta", Rules.min_ref_anchor_delta) is None
            else float(get_value(args, config, "min_ref_anchor_delta", Rules.min_ref_anchor_delta))
        ),
        max_ref_anchor_delta_fail_rate=float(
            get_value(
                args,
                config,
                "max_ref_anchor_delta_fail_rate",
                Rules.max_ref_anchor_delta_fail_rate,
            )
        ),
        screening_unit=str(get_value(args, config, "screening_unit", Rules.screening_unit)),
        missing_control_policy=str(
            get_value(args, config, "missing_control_policy", Rules.missing_control_policy)
        ),
        remove_statistical_outliers=bool(
            get_value(args, config, "remove_statistical_outliers", Rules.remove_statistical_outliers)
        ),
    )


def write_default_config(path: Path) -> None:
    rules = asdict(Rules())
    rules["hidden_ref_condition"] = list(DEFAULT_HIDDEN_REF_NAMES)
    rules["anchor_condition"] = list(DEFAULT_ANCHOR_NAMES)
    path.write_text(json.dumps(rules, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def run_analysis(args: argparse.Namespace) -> dict[str, Path]:
    config = parse_config(args.config)
    args.hidden_ref_condition = as_string_list(config.get("hidden_ref_condition")) + (
        args.hidden_ref_condition or []
    )
    args.anchor_condition = as_string_list(config.get("anchor_condition")) + (
        args.anchor_condition or []
    )
    rules = build_rules(args, config)
    if rules.screening_unit not in {"none", "listener", "trial", "both"}:
        raise ValueError("--screening-unit must be one of: none, listener, trial, both")
    if rules.missing_control_policy not in {"warn", "fail", "ignore"}:
        raise ValueError("--missing-control-policy must be one of: warn, fail, ignore")
    if rules.raw_score_max <= rules.raw_score_min:
        raise ValueError("--raw-score-max must be greater than --raw-score-min")
    if rules.max_score <= rules.min_score:
        raise ValueError("--max-score must be greater than --min-score")

    warnings: list[str] = []
    input_path = Path(args.input).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    rows, headers = read_table(input_path, sheet=args.sheet)
    ratings, condition_order, column_info = load_ratings(rows, headers, args, rules, warnings)
    listener_rows, trial_rows, waived_anchor_items = screen_ratings(ratings, rules, warnings)
    outliers = find_statistical_outliers(ratings)
    if rules.remove_statistical_outliers:
        remove_outliers(ratings, outliers)
        outliers = find_statistical_outliers(ratings)

    summaries_all = summarize_conditions(ratings, condition_order, include_controls=True)
    summaries_systems = summarize_conditions(ratings, condition_order, include_controls=False)

    outputs = {
        "all_scores_with_flags": output_dir / "all_scores_with_flags.csv",
        "clean_scores": output_dir / "clean_scores.csv",
        "clean_system_scores": output_dir / "clean_system_scores.csv",
        "listener_screening": output_dir / "listener_screening.csv",
        "trial_screening": output_dir / "trial_screening.csv",
        "condition_summary_all": output_dir / "condition_summary_all.csv",
        "condition_summary_systems": output_dir / "condition_summary_systems.csv",
        "statistical_outliers": output_dir / "statistical_outliers.csv",
        "boxplot": output_dir / "boxplot.svg",
        "boxplot_png": output_dir / "boxplot.png",
        "conclusion": output_dir / "conclusion.md",
        "effective_config": output_dir / "effective_config.json",
    }

    rating_fields = [
        "source_row",
        "source_column",
        "subject",
        "item",
        "condition",
        "role",
        "raw_score",
        "score",
        "invalid_score",
        "score_issue",
        "listener_excluded",
        "trial_excluded",
        "retained",
        "exclusion_reason",
    ]
    write_csv(outputs["all_scores_with_flags"], ratings, rating_fields)
    write_csv(outputs["clean_scores"], [row for row in ratings if row["retained"]], rating_fields)
    write_csv(
        outputs["clean_system_scores"],
        [row for row in ratings if row["retained"] and row["role"] == "system"],
        rating_fields,
    )
    write_csv(
        outputs["listener_screening"],
        listener_rows,
        [
            "subject",
            "trial_count",
            "rating_count",
            "hidden_ref_trials",
            "hidden_ref_fail_n",
            "hidden_ref_fail_rate",
            "anchor_trials",
            "anchor_fail_n",
            "anchor_fail_rate",
            "ref_anchor_delta_trials",
            "ref_anchor_delta_fail_n",
            "ref_anchor_delta_fail_rate",
            "excluded",
            "exclusion_reason",
        ],
    )
    write_csv(
        outputs["trial_screening"],
        trial_rows,
        [
            "subject",
            "item",
            "rating_count",
            "hidden_ref_score",
            "anchor_score_max",
            "anchor_score_mean",
            "missing_hidden_ref",
            "missing_anchor",
            "hidden_ref_fail",
            "anchor_fail_raw",
            "anchor_fail_waived",
            "anchor_fail",
            "ref_anchor_delta",
            "ref_anchor_delta_fail",
            "trial_fail",
            "trial_fail_reasons",
        ],
    )
    summary_fields = [
        "condition",
        "role",
        "system_rank_by_mean",
        "n",
        "subject_count",
        "item_count",
        "mean",
        "median",
        "std",
        "se",
        "ci95_low",
        "ci95_high",
        "min",
        "q1",
        "q3",
        "max",
        "iqr",
        "whisker_low",
        "whisker_high",
        "outlier_count",
        "outlier_values",
    ]
    write_csv(outputs["condition_summary_all"], summaries_all, summary_fields)
    write_csv(outputs["condition_summary_systems"], summaries_systems, summary_fields)
    write_csv(
        outputs["statistical_outliers"],
        outliers,
        [
            "subject",
            "item",
            "condition",
            "role",
            "score",
            "lower_fence",
            "upper_fence",
            "source_row",
            "source_column",
        ],
    )
    svg_boxplot(
        outputs["boxplot"],
        summaries_all,
        ratings,
        rules,
        include_controls=not args.plot_exclude_controls,
    )
    png_boxplot(
        outputs["boxplot_png"],
        summaries_all,
        ratings,
        rules,
        include_controls=not args.plot_exclude_controls,
    )
    effective = {
        "rules": asdict(rules),
        "columns": column_info,
        "waived_anchor_items": waived_anchor_items,
        "warnings": warnings,
    }
    outputs["effective_config"].write_text(
        json.dumps(effective, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    write_conclusion(
        outputs["conclusion"],
        ratings,
        listener_rows,
        trial_rows,
        summaries_all,
        summaries_systems,
        outliers,
        warnings,
        rules,
        column_info,
        outputs,
    )
    return outputs


def self_test() -> int:
    with tempfile.TemporaryDirectory(prefix="mushra-self-test-") as temp_dir:
        root = Path(temp_dir)
        input_path = root / "example.csv"
        output_dir = root / "out"
        input_path.write_text(
            "\n".join(
                [
                    "subject,item,condition,role,score",
                    "S1,item1,HiddenRef,hidden_reference,99",
                    "S1,item1,Anchor,anchor,18",
                    "S1,item1,SystemA,system,82",
                    "S1,item1,SystemB,system,71",
                    "S1,item2,HiddenRef,hidden_reference,98",
                    "S1,item2,Anchor,anchor,19",
                    "S1,item2,SystemA,system,84",
                    "S1,item2,SystemB,system,73",
                    "S2,item1,HiddenRef,hidden_reference,65",
                    "S2,item1,Anchor,anchor,18",
                    "S2,item1,SystemA,system,80",
                    "S2,item1,SystemB,system,69",
                    "S2,item2,HiddenRef,hidden_reference,95",
                    "S2,item2,Anchor,anchor,19",
                    "S2,item2,SystemA,system,81",
                    "S2,item2,SystemB,system,70",
                    "S3,item1,HiddenRef,hidden_reference,98",
                    "S3,item1,Anchor,anchor,25",
                    "S3,item1,SystemA,system,79",
                    "S3,item1,SystemB,system,68",
                    "S3,item2,HiddenRef,hidden_reference,96",
                    "S3,item2,Anchor,anchor,23",
                    "S3,item2,SystemA,system,80",
                    "S3,item2,SystemB,system,67",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        args = build_parser().parse_args(
            [str(input_path), "--output-dir", str(output_dir), "--disable-anchor-item-waiver"]
        )
        outputs = run_analysis(args)
        listener_text = outputs["listener_screening"].read_text(encoding="utf-8")
        clean_text = outputs["clean_system_scores"].read_text(encoding="utf-8")
        assert "S2" in listener_text and "True" in listener_text
        assert "S3" in listener_text and "True" in listener_text
        assert "SystemA" in clean_text and "S2" not in clean_text and "S3" not in clean_text
        assert outputs["boxplot"].exists()
    print("Self-test passed.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="处理 MUSHRA 主观听音评价数据，输出筛选结果、统计汇总、箱线图和结论。",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("input", nargs="?", help="原始数据文件：CSV/TSV/JSON/JSONL；XLSX 需 openpyxl。")
    parser.add_argument("-o", "--output-dir", default="mushra_results", help="输出目录。")
    parser.add_argument("--config", help="JSON 配置文件；键名与命令行参数一致，短横线可写为下划线。")
    parser.add_argument(
        "--input-format",
        choices=("auto", "long", "wide"),
        default="auto",
        help="long=一行一个评分；wide=一行一个 subject-item、多个条件列。",
    )
    parser.add_argument("--sheet", help="XLSX 工作表名；不填则读取活动工作表。")
    parser.add_argument("--subject-col", help="听音者列名。")
    parser.add_argument("--item-col", help="材料/项目列名。")
    parser.add_argument("--condition-col", help="条件/系统列名，long 格式使用。")
    parser.add_argument("--score-col", help="评分列名，long 格式使用。")
    parser.add_argument("--role-col", help="控制项角色列名，可取 anchor/hidden_reference/system。")
    parser.add_argument(
        "--wide-score-columns",
        help="宽表评分列，用逗号分隔；不填时自动从数值列推断。",
    )
    parser.add_argument(
        "--hidden-ref-condition",
        action="append",
        help="指定 hidden reference 条件名，可重复。",
    )
    parser.add_argument("--anchor-condition", action="append", help="指定 anchor 条件名，可重复。")

    parser.add_argument("--min-score", type=float, help="评分下限。")
    parser.add_argument("--max-score", type=float, help="评分上限。")
    parser.add_argument(
        "--raw-score-min",
        type=float,
        help="原始评分/标尺长度下限；将线性归一化到 --min-score。",
    )
    parser.add_argument(
        "--raw-score-max",
        type=float,
        help="原始评分/标尺长度上限；将线性归一化到 --max-score。",
    )
    parser.add_argument("--hidden-ref-min", type=float, help="Hidden Reference 合格最低分。")
    parser.add_argument(
        "--max-hidden-ref-fail-rate",
        type=float,
        help="听音者 hidden reference 失败率剔除阈值，如 0.15。",
    )
    parser.add_argument("--anchor-max", type=float, help="Anchor 合格最高分。")
    parser.add_argument(
        "--max-anchor-fail-rate",
        type=float,
        help="听音者 anchor 失败率剔除阈值，如 0.15。",
    )
    parser.add_argument(
        "--anchor-item-waiver-rate",
        type=float,
        help="同一材料 anchor 高分比例超过该值时，豁免该材料的 anchor 剔除依据。",
    )
    parser.add_argument(
        "--disable-anchor-item-waiver",
        action="store_true",
        help="关闭 anchor 材料级豁免。",
    )
    parser.add_argument(
        "--min-ref-anchor-delta",
        type=float,
        help="可选规则：HiddenRef - Anchor 必须不低于该差值；不设则禁用。",
    )
    parser.add_argument(
        "--max-ref-anchor-delta-fail-rate",
        type=float,
        help="可选差值规则的听音者失败率剔除阈值。",
    )
    parser.add_argument(
        "--screening-unit",
        choices=("none", "listener", "trial", "both"),
        help="剔除粒度：listener 按标准剔除整名听音者；trial 剔除 subject-item；both 两者都做。",
    )
    parser.add_argument(
        "--missing-control-policy",
        choices=("warn", "fail", "ignore"),
        help="缺少 Anchor/Hidden Reference 时的处理。",
    )
    parser.add_argument(
        "--remove-statistical-outliers",
        action="store_true",
        default=None,
        help="按 1.5*IQR 自动剔除统计异常值；默认只报告和绘图，不剔除。",
    )
    parser.add_argument(
        "--plot-exclude-controls",
        action="store_true",
        help="箱线图不绘制 Anchor 和 Hidden Reference。",
    )
    parser.add_argument(
        "--write-default-config",
        help="写出默认配置模板到指定路径后退出。",
    )
    parser.add_argument("--self-test", action="store_true", help="运行内置自检。")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.self_test:
        return self_test()
    if args.write_default_config:
        write_default_config(Path(args.write_default_config).expanduser().resolve())
        print(f"Default config written to {args.write_default_config}")
        return 0
    if not args.input:
        parser.error("the following arguments are required: input")

    try:
        outputs = run_analysis(args)
    except Exception as exc:  # noqa: BLE001 - command-line tool should report concise failures.
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print("MUSHRA analysis complete.")
    for label, path in outputs.items():
        print(f"{label}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

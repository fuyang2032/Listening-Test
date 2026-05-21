# MUSHRA Results Analysis

This folder contains a standalone Python script for processing MUSHRA subjective listening test data. It supports raw MUSHRA scores with Anchor, Hidden Reference, and system conditions, then outputs screening results, cleaned data, statistical summaries, boxplots with outliers, and a report-ready conclusion.

## Features

- Reads long-format CSV/TSV/JSON/JSONL data. XLSX is supported when `openpyxl` is installed.
- Normalizes raw subjective scores linearly to the ITU-R BS.1534 MUSHRA scale, usually `0-100`.
- Applies default post-screening rules:
  - Hidden Reference is qualified by default when score `>= 90`.
  - Anchor is qualified by default when score `<= 20`.
  - A listener is excluded when the Hidden Reference or Anchor failure rate is greater than `15%`.
  - Anchor item-level waiver is enabled by default when more than `25%` listeners rate the same anchor above threshold.
- Allows all thresholds to be customized from CLI or JSON config.
- Produces:
  - flagged full-score CSV
  - cleaned-score CSV
  - listener-level screening CSV
  - trial-level screening CSV
  - condition-level summary CSV
  - statistical outlier CSV
  - SVG and PNG boxplots
  - Markdown conclusion

## Folder Structure

```text
Results Analysis/MUSHRA/
├── README.md
├── scripts/
│   └── mushra_analyzer.py
├── tests/
│   └── test_mushra_analyzer.py
└── example_report/
    ├── raw_mushra_0_to_1.csv
    ├── evaluation_report.md
    └── analysis/
        ├── boxplot.svg
        ├── boxplot.jpg
        ├── condition_summary_systems.csv
        ├── listener_screening.csv
        ├── statistical_outliers.csv
        └── ...
```

## Quick Start

Run the analyzer on the included example data:

```bash
python3 "scripts/mushra_analyzer.py" \
  "example_report/raw_mushra_0_to_1.csv" \
  --output-dir "example_report/analysis" \
  --raw-score-min 0 \
  --raw-score-max 1
```

For ordinary `0-100` MUSHRA data, omit the raw score range:

```bash
python3 "scripts/mushra_analyzer.py" raw_mushra.csv -o mushra_results
```

## Input Format

Recommended long format:

```csv
subject,item,condition,role,score
S01,seq1,HiddenRef,hidden_reference,0.96
S01,seq1,Anchor,anchor,0.16
S01,seq1,CodecA,system,0.85
S01,seq1,CodecB,system,0.67
```

Required concepts:

- `subject`: listener or assessor ID
- `item`: audio material / trial / sequence ID
- `condition`: system or control condition name
- `role`: `hidden_reference`, `anchor`, or `system`
- `score`: raw subjective score

The script can infer many common column names automatically. If inference fails, specify them explicitly:

```bash
python3 "scripts/mushra_analyzer.py" raw.csv \
  --subject-col listener_id \
  --item-col sample_id \
  --condition-col system_name \
  --score-col rating \
  --role-col role
```

## Default Screening Rules

The default rule profile is designed for MUSHRA-style post-screening and can be customized:

| Rule | Default |
|---|---:|
| Normalized score range | `0-100` |
| Hidden Reference pass threshold | `>= 90` |
| Anchor pass threshold | `<= 20` |
| Max Hidden Reference failure rate per listener | `15%` |
| Max Anchor failure rate per listener | `15%` |
| Anchor item-level waiver rate | `25%` |
| Screening unit | `listener` |

Example with custom thresholds:

```bash
python3 "scripts/mushra_analyzer.py" raw_mushra.csv \
  --hidden-ref-min 95 \
  --anchor-max 15 \
  --max-hidden-ref-fail-rate 0.10 \
  --max-anchor-fail-rate 0.10 \
  --screening-unit both
```

Write a default JSON config template:

```bash
python3 "scripts/mushra_analyzer.py" --write-default-config mushra_config.json
```

## Outputs

The analyzer writes these files to the output directory:

| File | Description |
|---|---|
| `all_scores_with_flags.csv` | All normalized scores with screening flags |
| `clean_scores.csv` | Retained scores after screening |
| `clean_system_scores.csv` | Retained system-condition scores only |
| `listener_screening.csv` | Listener-level control-item screening details |
| `trial_screening.csv` | Subject-item control-item details |
| `condition_summary_all.csv` | Summary for controls and systems |
| `condition_summary_systems.csv` | Summary for systems only |
| `statistical_outliers.csv` | 1.5 x IQR outlier list |
| `boxplot.svg` | SVG boxplot, with Heiti/SimHei font stack |
| `boxplot.png` | PNG boxplot fallback |
| `conclusion.md` | Auto-generated conclusion |
| `effective_config.json` | Effective rules and inferred columns |

## Example Result

The included example data uses a `0-1` raw score scale and is normalized to `0-100`.

Expected screening behavior:

- `S02` is excluded because Hidden Reference failure rate is `25% > 15%`.
- `S03` is excluded because Anchor failure rate is `25% > 15%`.
- `CodecB` has one statistical outlier at `15`, shown in the boxplot.

System summary from the example:

| Condition | Mean | Median | Notes |
|---|---:|---:|---|
| CodecA | `82.8333` | `83.25` | Highest mean |
| CodecB | `66.5417` | `66.50` | Contains one low outlier |

See [`example_report/evaluation_report.md`](example_report/evaluation_report.md) for a report-style output.

## Test

Run the regression test:

```bash
python3 -m unittest discover -s tests
```

Or run the analyzer self-test:

```bash
python3 "scripts/mushra_analyzer.py" --self-test
```

## Dependencies

The analyzer uses only the Python standard library for CSV/TSV/JSON/JSONL input, statistics, and SVG/PNG boxplot generation.

Optional:

- `openpyxl` is only needed for `.xlsx` input.


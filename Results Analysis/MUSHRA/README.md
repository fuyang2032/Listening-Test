# MUSHRA 结果分析工具

本目录提供一套独立的 Python 脚本，用于处理 MUSHRA 主观听音评价数据。脚本支持包含 Anchor、Hidden Reference 和系统评价项的原始数据，能够输出听音者筛选结果、清洗后的评分、统计汇总、包含异常值的箱线图，以及可直接放入报告的结论。

## 功能特性

- 支持读取长表格式的 CSV、TSV、JSON、JSONL 数据；如果安装了 `openpyxl`，也可以读取 XLSX。
- 按 ITU-R BS.1534 的 MUSHRA 标尺，将原始主观评分线性归一化到 `0-100`。
- 内置默认后筛规则：
  - Hidden Reference 默认合格阈值为 `>= 90`。
  - Anchor 默认合格阈值为 `<= 20`。
  - 当某位听音者的 Hidden Reference 或 Anchor 失败率超过 `15%` 时，剔除该听音者。
  - Anchor 材料级豁免默认开启：同一材料中超过 `25%` 听音者给 Anchor 打分高于阈值时，该材料的 Anchor 失败不计入听音者剔除。
- 所有阈值均支持通过命令行参数或 JSON 配置文件自定义。
- 输出内容包括：
  - 带筛选标记的全量评分 CSV
  - 后筛后的干净评分 CSV
  - 听音者级筛选明细
  - subject-item 级控制项明细
  - 条件级统计汇总
  - 1.5 x IQR 统计异常值列表
  - SVG 和 PNG 箱线图
  - Markdown 结论

## 目录结构

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

## 快速开始

使用随附测试数据运行分析：

```bash
python3 "scripts/mushra_analyzer.py" \
  "example_report/raw_mushra_0_to_1.csv" \
  --output-dir "example_report/analysis" \
  --raw-score-min 0 \
  --raw-score-max 1
```

如果原始数据已经是普通 `0-100` MUSHRA 评分，可以省略原始评分范围：

```bash
python3 "scripts/mushra_analyzer.py" raw_mushra.csv -o mushra_results
```

## 输入格式

推荐使用长表格式：

```csv
subject,item,condition,role,score
S01,seq1,HiddenRef,hidden_reference,0.96
S01,seq1,Anchor,anchor,0.16
S01,seq1,CodecA,system,0.85
S01,seq1,CodecB,system,0.67
```

字段含义：

- `subject`：听音者 / 评价者 ID
- `item`：音频材料 / trial / sequence ID
- `condition`：系统条件或控制项名称
- `role`：条件角色，可取 `hidden_reference`、`anchor`、`system`
- `score`：原始主观评分

脚本会自动识别常见列名。如果自动识别失败，可以显式指定：

```bash
python3 "scripts/mushra_analyzer.py" raw.csv \
  --subject-col listener_id \
  --item-col sample_id \
  --condition-col system_name \
  --score-col rating \
  --role-col role
```

## 默认筛选规则

默认规则面向 MUSHRA 后处理场景，也可以按实验要求调整：

| 规则 | 默认值 |
|---|---:|
| 归一化评分范围 | `0-100` |
| Hidden Reference 合格阈值 | `>= 90` |
| Anchor 合格阈值 | `<= 20` |
| 听音者 Hidden Reference 最大失败率 | `15%` |
| 听音者 Anchor 最大失败率 | `15%` |
| Anchor 材料级豁免比例 | `25%` |
| 默认筛选粒度 | `listener` |

自定义阈值示例：

```bash
python3 "scripts/mushra_analyzer.py" raw_mushra.csv \
  --hidden-ref-min 95 \
  --anchor-max 15 \
  --max-hidden-ref-fail-rate 0.10 \
  --max-anchor-fail-rate 0.10 \
  --screening-unit both
```

写出默认 JSON 配置模板：

```bash
python3 "scripts/mushra_analyzer.py" --write-default-config mushra_config.json
```

## 输出文件

脚本会在输出目录中生成以下文件：

| 文件 | 说明 |
|---|---|
| `all_scores_with_flags.csv` | 全量归一化评分及筛选标记 |
| `clean_scores.csv` | 后筛后保留的全部评分 |
| `clean_system_scores.csv` | 后筛后保留的系统条件评分 |
| `listener_screening.csv` | 听音者级控制项筛选明细 |
| `trial_screening.csv` | subject-item 级控制项明细 |
| `condition_summary_all.csv` | 控制项与系统条件的整体统计 |
| `condition_summary_systems.csv` | 仅系统条件的统计汇总 |
| `statistical_outliers.csv` | 按 1.5 x IQR 识别的异常值列表 |
| `boxplot.svg` | SVG 箱线图，字体栈包含黑体 / SimHei |
| `boxplot.png` | PNG 箱线图兜底版本 |
| `conclusion.md` | 自动生成的处理结论 |
| `effective_config.json` | 实际生效的规则与自动识别列信息 |

## 示例结果

随附示例数据使用 `0-1` 原始评分，并归一化到 `0-100`。

预期筛选行为：

- `S02` 因 Hidden Reference 失败率为 `25% > 15%` 被剔除。
- `S03` 因 Anchor 失败率为 `25% > 15%` 被剔除。
- `CodecB` 存在一个 `15` 分统计异常值，并在箱线图中显示。

示例系统统计结果：

| 条件 | 均值 | 中位数 | 说明 |
|---|---:|---:|---|
| CodecA | `82.8333` | `83.25` | 均值最高 |
| CodecB | `66.5417` | `66.50` | 包含一个低分异常值 |

报告样例见 [`example_report/evaluation_report.md`](example_report/evaluation_report.md)。

## 测试

运行回归测试：

```bash
python3 -m unittest discover -s tests
```

或运行脚本内置自检：

```bash
python3 "scripts/mushra_analyzer.py" --self-test
```

## 依赖

CSV、TSV、JSON、JSONL 输入、统计处理和 SVG/PNG 箱线图生成均只依赖 Python 标准库。

可选依赖：

- 仅当需要读取 `.xlsx` 文件时，才需要安装 `openpyxl`。

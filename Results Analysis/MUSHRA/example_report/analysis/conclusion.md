# MUSHRA 主观评价数据处理结论

生成时间：2026-05-21 14:14:19

## 采用的默认/有效规则

- 原始评分按 ITU-R BS.1534-3 的线性换算归一化：[0, 1] -> [0, 100]；Hidden Reference 低于 90 记为失败；Anchor 高于 20 记为失败。
- 听音者剔除阈值：Hidden Reference 失败率 > 15.00%，Anchor 失败率 > 15.00%。
- Anchor 材料豁免：同一材料中超过 25.00% 的听音者给 Anchor 高于 20 时，该材料的 Anchor 失败不计入听音者剔除。
- Hidden Reference 与 Anchor 差值规则：未启用。
- 筛选粒度：listener；缺失控制项处理：warn。

## 数据概况

- 输入格式识别：long。
- 原始评分行数：128；有效保留评分行数：96。
- 原始听音者数：8；保留听音者数：6。
- 原始材料数：4；保留材料数：4。
- 非数值/越界评分行数：0。
- 剔除听音者数：2；剔除 subject-item 组合数：0。
- 统计箱线图异常值数量：1。

## 被剔除听音者

- S02：hidden_ref_fail_rate 25.00% > 15.00%
- S03：anchor_fail_rate 25.00% > 15.00%

## 系统条件结论

- 按均值排序，当前最高的系统条件是 CodecA，均值 82.8333，中位数 83.25，n=24。
- 第二名是 CodecB，均值 66.5417；与第一名均值差约 16.2917。
- 完整均值、四分位数、95% 近似置信区间和异常值数量见 condition_summary*.csv。

## 输出文件

- all_scores_with_flags: `all_scores_with_flags.csv`
- clean_scores: `clean_scores.csv`
- clean_system_scores: `clean_system_scores.csv`
- listener_screening: `listener_screening.csv`
- trial_screening: `trial_screening.csv`
- condition_summary_all: `condition_summary_all.csv`
- condition_summary_systems: `condition_summary_systems.csv`
- statistical_outliers: `statistical_outliers.csv`
- boxplot: `boxplot.svg`
- boxplot_png: `boxplot.png`
- conclusion: `conclusion.md`
- effective_config: `effective_config.json`

说明：箱线图异常值按 1.5×IQR 标记并保留在图中；除非启用 `--remove-statistical-outliers`，这些统计异常值不会被自动剔除。

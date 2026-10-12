# T11 专项评审｜Research Dossier

## 目标

把前面所有已完成研究结果整理为一个可携带、可审计的研究资料包，供人工进行最终判断。

## 开源复用

参考开源 Amazon research 项目成熟的交付结构：

`raw/data → CSV/JSON → Markdown report → HTML dashboard`

本项目不复制其复杂评分/自动决策；报告生成保持标准库 + ECharts 的最小实现。

## 输出结构

```text
Research Dossier/
├── report.md
├── dashboard.html
├── data.json
├── source_index.json
├── manifest.json
└── csv/
    ├── market.csv
    ├── segments.csv
    ├── seller_regions.csv
    ├── market_decomposition.csv
    ├── concentration.csv
    ├── rank_layers.csv
    ├── head_suction.csv
    ├── new_product_outcomes.csv
    └── new_product_account_types.csv
```

## 可追溯性

- `source_index.json`：外部原始数据/附件来源索引，可保存 SHA256。
- `manifest.json`：对 Dossier 内每个生成文件做 SHA256，方便交付后验证未被修改。
- `data.json` 保存所有结构化结果，报告和 Dashboard 都可以重新生成。

## 最终决策边界

- `data.json` 明确 `decision_owner=human`。
- 系统不生成自动 decision 字段。
- Markdown / Dashboard 仅呈现研究事实、分析和后续研究路线。

## 评审结论

- ✅ 不依赖复杂报表服务。
- ✅ Markdown / CSV 可人工复核，JSON 可机器复用，HTML 可演示。
- ✅ Dossier 自带来源索引和输出文件哈希。
- ✅ 最终 Yes/No 没有被程序自动化。

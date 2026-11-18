# 输入与阅读记录

文件采用 UTF-8 JSONL，每行一个 JSON 对象。下面定义输入和阅读记录的字段，并附完整结构示例。

## 论文输入

| 字段 | 类型与含义 |
| --- | --- |
| `paper_id` | 非空字符串，沿用上游稳定标识 |
| `title`、`venue`、`year` | 标题、来源名称、四位整数年份 |
| `paper_url` | 公开 HTTP(S) 地址或 `null` |
| `abstract` | 完整摘要字符串或 `null` |
| `tags` | 已有标签的字符串数组；没有标签用 `[]` |
| `source_eligible` | 上游核对来源资格的结果：`true`、`false`、`null` |
| `sources` | 已取得的原文材料数组，没有正文材料时用 `[]` |
| `pdf_urls` | 可选，公开 PDF 地址数组 |

每项来源包含 `source_id`、`kind`、`url`、`text`。`kind` 为 `metadata`、`paper`、`repository`、`data`、`model` 或 `prior_note`；`text` 是实际来源文字，`url` 可为空。来源标识在一篇论文内唯一。程序另从标题、摘要和标签生成标识为 `metadata` 的来源，因此输入不要占用这个标识。论文文本和仓库文本分别保留，不把生成的摘要当原文。

需要复用既有笔记时，可以将去除旧等级、分数和入选结论的事实线索以 `prior_note` 提供，同时附上底层原文。笔记用于定位和减少重复查找，不能作为引用的原文来源。来源清单会保留其类别和摘要哈希。

已有来源的正文通过输入提供。仅有标题和摘要的记录可以预读，精读推荐需要相关正文。没有 DOI 也可以使用论文标识。来源未核对时填写 `source_eligible: null`。

## 两个阶段共用的阅读字段

| 字段 | 类型与含义 |
| --- | --- |
| `paper_id`、`stage` | 原样标识；阶段为 `triage` 或 `detail` |
| `model` | 执行模型标识，由程序根据本批运行配置填写，阅读者无需输出 |
| `reading_scope` | `metadata_only`、`partial_text`、`paper_text` 或 `paper_and_repository` |
| `main_content` | 主要内容的简短说明，包含本次关注的重要主要结论 |
| `reason` | 阅读优先级或精筛判断的综合理由 |
| `evidence` | 有依据的引用数组，格式见下文 |
| `extra_sources` | 阅读中补取的来源，使用 `source_id`、`kind`、`url`、`path`；`path` 指向工作目录内已取得的 UTF-8 文本文件。没有则为 `[]` |
| `next_checks` | 具体待查事项数组；没有则为 `[]` |

`paper_text` 表示判断所需的相关正文已读，不表示全部附录均已读。重要内容仍缺失时用 `partial_text`。`paper_and_repository` 表示相关正文和仓库材料都已阅读，不表示代码运行成功。

每项 `next_checks` 包含 `question` 字符串和 `blocking` 布尔值。会改变来源适配、等级或能否推荐的关键未知项标为 `true`；仅需在启动实验前确定的细节标为 `false`。有阻断项的记录保留为待补查，已有明确不适配证据的记录仍暂缓。不要列泛泛的注意事项。

## 预读输出

预读使用全部共用字段，并增加 `decision`。阅读范围固定为 `metadata_only`，依据完整标题摘要和标签判断阅读优先级，不输出四维等级或数值总分。

| 预读判断 | 含义 |
| --- | --- |
| `priority` | 主要内容集中，有望在本批条件下检验主要结论，优先安排精读 |
| `possible` | 相关且可能可用，但主要内容、准备工作、年代等存在具体取舍，阅读优先级稍后 |
| `needs_evidence` | 某个具体未知事实妨碍判断主要贡献或是否值得优先精读，注明需要查什么；未检查仓库本身不使所有论文都归入此档 |
| `defer` | 已有文字明确支持不适配判断，例如方向不相关、违反本批准入条件或准备工作明显超出资源限制；须附依据 |

这些含义用于安排阅读，不直接对应四维等级。精读后重新依据正文和四维条件分档。来源资格明确不符，或阅读者已有依据判为 `defer`，程序保留暂缓；其余记录中，资格未知或存在阻断项时归为待补查。

## 精读输出

精读使用全部共用字段，再增加下面的字段，不输出 `decision`。程序从条件、证据准备状态和等级推导档次。

| 字段 | 内容 |
| --- | --- |
| `extracted` | `topics`、`contribution_types`、`models`、`datasets`、`metrics` 为字符串数组或 `null`；`method` 为方法概述或 `null` |
| `in_scope` | 主题是否符合本批范围：`true`、`false`、`null` |
| `training` | 候选验证是否需要新增参数训练：`none`、`required`、`unknown` |
| `benchmark_primary` | 是否以构建 Benchmark 为主要贡献：`true`、`false`、`null` |
| `implementation` | 实现材料与路径，见下文 |
| `feasibility_note` | 简短可行性构想或 `null`，不要求完整实验设计 |
| `criteria` | 恰好包含 A、B、C、D；每项为 `level` 与 `reason` |

`extracted` 记录原文相关实验使用的模型、数据和指标，不能写成已经决定的复现实验配置。`null` 表示尚未建立，`[]` 表示已确认没有该项。论文类型和研究方向可以重叠，不用单个类型标签自动推导 Benchmark 主贡献。

`implementation` 包含以下四项：

- `url`：作者实现的公开入口或 `null`。
- `status`：`inspected` 表示已检查相关实现；`linked` 表示入口可访问、实现内容尚未细查；`claimed` 表示仅有发布承诺；`not_found` 表示本次检查未找到作者实现；`unknown` 表示尚未核查。前两种状态必须有地址。
- `route`：`author_code`、`paper_based`、`mixed`、`unknown`，分别表示依托作者代码、依据论文实现、二者结合、路径尚不明确。没有作者仓库且有论文支持的实现路径可以推荐。仅有发布承诺时，依托作者代码的路径尚未建立。
- `preparation`：影响选择的主要准备工作说明或 `null`，不列完整安装步骤。

`criteria` 的每个 `level` 为整数 0、1、2、3 或 `null`，布尔值不算整数等级。每项都写简短 `reason`；已评分项必须有证据支持。只有元数据的精读记录全部等级为空。关键正文仍未读全的记录可以保留已经判断的等级，但不能进入最终排序。

本批适配条件已经确认不满足时，可以将四项均留空并说明原因。已知的 0 分障碍优先于其他未知项，记录归为暂缓。所有等级已知时程序计算总分，但只有满足推荐准备条件的优先和可选记录进入排序。

## 证据格式

每项引用包含四个字段：

| 字段 | 要求 |
| --- | --- |
| `source_id` | 指向输入或补充来源 |
| `locator` | 原文位置，如章节、表图、PDF 页码、代码文件及函数；元数据可写标题或摘要 |
| `quote` | 实际连续原文片段，保留足以理解判断的上下文 |
| `supports` | 支持的字段数组，取 `main_content`、`in_scope`、`training`、`benchmark_primary`、`implementation`、`extracted`、A、B、C、D |

主要内容、已确定的适配判断、已建立的实现路径及每个非空等级都要有相应引用。可以让一条引用支持多个判断，但其语义必须足以支持。程序容许换行、连续空白、全半角和常见引号差异，核对片段确实出现在登记来源中；不会证明来源真实性或引用的语义充分性。复核需要回到原文。

推荐准备条件包括相关正文已读、至少一项来自 `paper` 来源的引用、明确实现路径及没有阻断性未知项。仅有仓库或摘要引用不能代替正文依据。发现来源资格冲突时标记阻断性待查项，交由协调者核对输入资格。

## 程序结果与最终选择

阅读者不得填写 `score`、`tier`、`rankable` 或排序位置。执行器收取记录时，读取实际来源文件，核对引用并计算归一化分数和档次。保存的记录保留判断、原文引用、来源清单与文本哈希，移除 `extra_sources` 的文件内容和临时路径。下游读取这份已核对记录，不依赖已删除的工作目录。缺失记录、无效引用和记录改动都会阻止完整导出。

`ranked.jsonl` 只含可进入名单讨论的精读记录，先优先、后可选，各档按分数排序。待补查和暂缓单独保存。`analysis.jsonl` 提供固定类型的查询字段，包括来源、年份、档次、四维等级、总分、材料状态和提取内容。总分 `score` 为 0 到 1 的浮点数或 `null`；等级仍为整数。执行失败记录在尝试状态中，不产生论文分数。

最终选择文件是 JSON 数组，每项仅含 `paper_id` 和 `reason`。`export` 从合格的精读记录中取出指定论文，保留选择文件中的顺序和理由。

## 完整记录结构示例

以下两份记录只展示字段结构，不能直接提交为真实阅读结果。假设输入标识为 `example-format`，摘要为 `This is fictional metadata for a format example.`；阅读者尚未取得足以判断主要内容的正文。实际任务应按真实来源填写字段和等级。JSONL 写入时每条记录占一行。

以下为程序补入 `model` 前的阅读输出。API 方式的 `extra_sources` 由程序登记，评分时输出 `[]`；Codex 方式按补取文件填写。

预读记录：

```json
{
  "paper_id": "example-format",
  "stage": "triage",
  "reading_scope": "metadata_only",
  "main_content": "仅有虚构元数据，主要贡献尚待明确。",
  "reason": "需要正文信息才能判断阅读优先级。",
  "decision": "needs_evidence",
  "evidence": [
    {"source_id": "metadata", "locator": "摘要", "quote": "This is fictional metadata for a format example.", "supports": ["main_content"]}
  ],
  "extra_sources": [],
  "next_checks": [{"question": "读取论文主要贡献和实验概述。", "blocking": true}]
}
```

精读阶段尚缺正文的记录：

```json
{
  "paper_id": "example-format",
  "stage": "detail",
  "reading_scope": "metadata_only",
  "main_content": "仅有虚构元数据，主要贡献尚待明确。",
  "reason": "相关正文未取得，保留未知项并补查。",
  "extracted": {"topics": null, "contribution_types": null, "method": null, "models": null, "datasets": null, "metrics": null},
  "in_scope": null,
  "training": "unknown",
  "benchmark_primary": null,
  "implementation": {"url": null, "status": "unknown", "route": "unknown", "preparation": null},
  "feasibility_note": null,
  "criteria": {
    "A": {"level": null, "reason": "尚未取得主要结论的比较与评价依据。"},
    "B": {"level": null, "reason": "尚未确认主要内容。"},
    "C": {"level": null, "reason": "关键实验条件尚未读到。"},
    "D": {"level": null, "reason": "实现材料和准备工作尚未建立。"}
  },
  "evidence": [
    {"source_id": "metadata", "locator": "摘要", "quote": "This is fictional metadata for a format example.", "supports": ["main_content"]}
  ],
  "extra_sources": [],
  "next_checks": [{"question": "取得并阅读相关正文与材料入口。", "blocking": true}]
}
```

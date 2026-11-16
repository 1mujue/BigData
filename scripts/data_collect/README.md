# 论文收集模块

按[仓库环境配置](../../README.md#环境配置)准备运行环境。以下命令从仓库根目录执行。采集与验证需要访问 DBLP SPARQL、OpenAlex 和公开 PDF 托管站点。

## 数据流程

| 阶段 | 输入与输出 | 工具 |
| --- | --- | --- |
| 元数据采集 | DBLP 会议或期刊标识与年份范围 → 每篇一行的 JSONL | Python、DBLP SPARQL |
| 入库和整理 | 已校验的 JSONL → HDFS 批次 → 去重后的 Parquet | Docker Compose、HDFS、PySpark |
| 查找和下载 | 根据论文条目和公开来源取得 `<paper_id>.pdf` | 官方站点、OpenAlex、Researchr、Firecrawl |
| 文本处理 | PDF → 每页一行的 JSONL → HDFS 页记录 | PyMuPDF、HDFS |
| 全文检索 | HDFS 页记录 → ES 页文档 → 页码和高亮 | PySpark、Elasticsearch |

`dblp_id` 是原始 DBLP 记录标识。`paper_id` 是该标识的 SHA-256，用来关联元数据、PDF 和逐页文本。Elasticsearch 的文档 ID 采用 `paper_id:page`，重跑同一篇同页数的 PDF 时会更新对应页文档。

## 自动批量采集 PDF 并入库

安装 `requirements.txt`，启动下述 HDFS 服务后，从项目根目录运行：

```bash
python -u -m scripts.data_collect collect-pdfs
```

默认采集 [venues.json](venues.json) 中 13 个会议、期刊的 2024 至 2026 年 DBLP 条目，未收录的届次跳过。PDF 依次从官方来源、OpenAlex 公开地址、Researchr 作者副本和 Firecrawl 搜索结果获取；会议及 Researchr 年度入口统一在配置文件中维护。

Firecrawl 使用仓库根目录 `.env` 或进程环境中的配置，`.env` 已被 Git 忽略：

```dotenv
FIRECRAWL_API_KEY=你的密钥
FIRECRAWL_BASE_URL=https://api.firecrawl.dev/v2
```

OpenAlex 不需要额外 key。代理使用标准 `HTTP_PROXY`、`HTTPS_PROXY`、`ALL_PROXY`，`NO_PROXY` 应包含 `127.0.0.1,localhost`；进程环境优先于 `.env`。

限定会议、年份和采集数量：

```bash
python -u -m scripts.data_collect collect-pdfs \
  --venues ICLR ICSE FSE --years 2024 2025 --limit-per-venue 2 \
  --output data/collection-check --hdfs-root /papers/collection/check
```

不传 `--limit-per-venue` 就采集选定范围的全部条目。相同命令可续跑，已入库文件跳过，搜索结果复用缓存；改变采集范围时使用新的输出目录。

Firecrawl 默认没有查询总数上限，每篇缺失论文搜索一次并尝试最多 5 个结果，无可用文件就记录并跳过。可显式用 `--max-searches` 限定新增查询数，`0` 表示只复用缓存。默认 4 个下载线程，同一主机限速，连续受阻时转向其他来源。

本地默认目录为 `data/collection`：`raw/` 保存 DBLP 批次，`downloads.jsonl` 记录进度，`manifest.jsonl` 和 `summary.json` 保存结果及错误。HDFS 默认根目录为 `/papers/collection/main`：`pdfs/` 保存 PDF，`tables/papers/` 和 `tables/downloads/` 保存通过 `paper_id` 关联的 Parquet 表。文件上传并核对后清理本地暂存，上传失败则保留。

## 启动 HDFS 和 Elasticsearch

从 **项目根目录** 运行。HDFS 由一个 NameNode 和一个 DataNode 组成，Elasticsearch 使用单节点 Docker 配置。各服务的数据保存在 Docker 命名卷中。

### 首次初始化空 HDFS 卷

以下两次 `chown` 将卷根目录交给镜像内的 `hadoop` 用户，`namenode -format` 在空卷上初始化元数据。**已有 HDFS 数据的环境直接使用下一节的启动命令。**

```bash
docker compose -f scripts/data_collect/docker/compose.yaml config -q

docker compose -f scripts/data_collect/docker/compose.yaml run --rm --no-deps \
  --user root --entrypoint chown namenode hadoop /data
docker compose -f scripts/data_collect/docker/compose.yaml run --rm --no-deps \
  --user root --entrypoint chown datanode hadoop /data
docker compose -f scripts/data_collect/docker/compose.yaml run --rm --no-deps \
  namenode hdfs namenode -format -nonInteractive
```

### 启动和检查服务

```bash
docker compose -f scripts/data_collect/docker/compose.yaml up -d namenode datanode
docker compose -f scripts/data_collect/docker/compose.yaml exec -T --interactive=false namenode \
  cat /proc/sys/vm/max_map_count
```

Elasticsearch 的 Docker 主机应满足 `vm.max_map_count >= 1048576`。若 Docker Engine 直接运行在 Ubuntu WSL 中，值偏低时在该发行版执行 `sudo sysctl -w vm.max_map_count=1048576`。若使用 Docker Desktop 的 WSL2 后端，在 Docker Desktop 使用的内核环境中按 Elastic 的官方说明调整。 [Elastic Docker 配置](https://www.elastic.co/docs/deploy-manage/deploy/self-managed/install-elasticsearch-docker-prod#set-vm-max-map-count)

```bash
docker compose -f scripts/data_collect/docker/compose.yaml up -d elasticsearch
docker compose -f scripts/data_collect/docker/compose.yaml ps

docker compose -f scripts/data_collect/docker/compose.yaml exec -T --interactive=false namenode \
  hdfs dfsadmin -report

docker compose -f scripts/data_collect/docker/compose.yaml exec -T --interactive=false namenode \
  hdfs dfsadmin -safemode get

curl --fail --silent --show-error http://127.0.0.1:9200/
```

运行验证前，应看到 `Live datanodes (1)`、`Safe mode is OFF` 和 Elasticsearch 的版本响应。容器刚启动时可以稍等再检查。

| 端口 | 用途 |
| --- | --- |
| `8020` | NameNode RPC，供 WSL 中的 Spark 查询 HDFS |
| `9866` | DataNode 数据块传输 |
| `9870` | NameNode 管理页面 |
| `9200` | Elasticsearch HTTP API |

四个映射都绑定在 `127.0.0.1`。Elasticsearch 关闭认证以便本机课程实验。Docker 命名卷保存已有数据，日常可以用 `docker compose -f scripts/data_collect/docker/compose.yaml stop` 停止服务，再用 `up -d` 启动。

## 原有流程样例

```bash
bash scripts/data_collect/workflows/verify_pipeline.sh
```

脚本以 Litmus 论文验证 DBLP 采集、HDFS 存储、Spark 整理、PDF 页文本提取和 ES 检索。具体步骤见 [verify_pipeline.sh](workflows/verify_pipeline.sh)，输出保存在 `data/verification/` 与 HDFS `/papers/verification/`。

## 分步使用

### 按期刊和发表年份查询元数据

运行 `python -m scripts.data_collect --help` 查看所有子命令。DBLP 查询使用 `collect` 子命令。`--stream` 使用 DBLP 的期刊或会议标识，例如 IEEE TKDE 的标识是其 DBLP 目录路径中的 `journals/tkde`。 [IEEE TKDE 的 DBLP 目录](https://dblp.org/db/journals/tkde/)

```bash
# 查询 2020–2024 年的 IEEE TKDE，取前 50 条
python -m scripts.data_collect collect \
  --stream journals/tkde --start-year 2020 --end-year 2024 \
  --limit 50 --output data/raw/tkde-2020-2024.jsonl

# 查询 SIGMOD 2022 的全部 DBLP 记录
python -m scripts.data_collect collect \
  --stream conf/sigmod --year 2022 --all \
  --output data/raw/sigmod-2022.jsonl
```

- 单年用 `--year`，跨年用 `--start-year` 和 `--end-year`。范围包含起止年份，例如 2010 年代对应 2010 至 2019 年。
- `--limit` 是整个查询去重后的篇数上限，默认为 20。按 DBLP 标识排序，取满后结束。`--all` 会分页取完整结果。`--page-size` 控制每页的论文数，同一篇的全部作者和链接保留在一起；`--doi` 可进一步限定到一个 DOI。
- 输出是本地 JSONL，每行包含 DBLP 标识、标题、年份、发表场所、DOI、作者名单（按姓名排序）、文档链接、TOC 卷链接和出版类型。缺失 DOI 时该字段为 `null`。

### 采集并入库单年批次

```bash
bash scripts/data_collect/workflows/ingest_metadata.sh conf/sigmod 2022 25 10
```

批次入库前由 Spark 校验行数、唯一 ID 和必填字段，入库文件以内容的 SHA-256 命名。同样内容的批次可重跑。

### 从多个批次生成论文表

首次由 WSL 里的 Spark 写入整理目录时，给运行用户准备写权限。

```bash
docker compose -f scripts/data_collect/docker/compose.yaml exec -T --interactive=false namenode \
  hdfs dfs -mkdir -p /papers/metadata/curated
docker compose -f scripts/data_collect/docker/compose.yaml exec -T --interactive=false namenode \
  hdfs dfs -chown "$(id -un)" /papers/metadata/curated

python -m scripts.data_collect build-metadata \
  hdfs://127.0.0.1:8020/papers/metadata/batches/conf/sigmod/2022 \
  hdfs://127.0.0.1:8020/papers/metadata/curated/sigmod-2022-v1
```

同一 `dblp_id` 优先保留带 DOI 的记录。再次生成时用新的 Parquet 路径，例如 `sigmod-2022-v2`。

### 查找公开链接、下载和索引

```bash
python -m scripts.data_collect resolve-pdfs \
  hdfs://127.0.0.1:8020/papers/metadata/curated/sigmod-2022-v1 \
  data/processed/pdf-manifest.jsonl

paper_id='从清单复制已核对的 paper_id'
python -m scripts.data_collect download-pdf \
  data/processed/pdf-manifest.jsonl data/raw/pdfs "$paper_id"

python -m scripts.data_collect extract-pages \
  "data/raw/pdfs/$paper_id.pdf" "data/processed/pages/$paper_id.jsonl"

docker compose -f scripts/data_collect/docker/compose.yaml exec -T --interactive=false namenode \
  hdfs dfs -mkdir -p /papers/raw/pdfs /papers/processed/pages
docker compose -f scripts/data_collect/docker/compose.yaml exec -T namenode \
  hdfs dfs -put -f - "/papers/raw/pdfs/$paper_id.pdf" < "data/raw/pdfs/$paper_id.pdf"
docker compose -f scripts/data_collect/docker/compose.yaml exec -T namenode \
  hdfs dfs -put -f - "/papers/processed/pages/$paper_id.jsonl" \
  < "data/processed/pages/$paper_id.jsonl"

python -m scripts.data_collect index-pages \
  "hdfs://127.0.0.1:8020/papers/processed/pages/$paper_id.jsonl" "$paper_id"
```

`resolve-pdfs` 分批查询全部记录，保留 OpenAlex 的全部公开候选；可用 `--limit` 限定数量，无 DOI 的记录仍保留在清单中。上述单篇命令用于检查具体文件；自动回退、批量下载和入库使用 `collect-pdfs`。

## 目录结构

| 路径 | 职责 |
| --- | --- |
| [__main__.py](__main__.py) | Python 命令入口，解析参数并调用对应模块。 |
| [metadata.py](metadata.py) | DBLP 元数据查询、Spark 元数据校验和 Parquet 表整理。 |
| [pdf.py](pdf.py) | OpenAlex 链接解析、共用 PDF 下载与校验、正文提取。 |
| [sources.py](sources.py) | 官方目录、Researchr、Firecrawl 和网页链接解析。 |
| [batch.py](batch.py) | 批量任务、续跑、HDFS 上传和结果表。 |
| [network.py](network.py) | 共用 HTTP 请求、主机限速和有限重试。 |
| [index.py](index.py) | 读取 HDFS 页记录，写入 Elasticsearch 并验证短语命中。 |
| [tool.py](tool.py) | 共享的 JSONL 写入和 Spark 会话管理。 |
| [workflows/](workflows/) | 单年批次入库和端到端验证的 Bash 工作流。 |
| [docker/](docker/) | HDFS 和 Elasticsearch 的 Compose 配置。 |

本地下载和验证结果位于仓库根目录的 `data/`，由 Git 忽略。Compose 项目名为 `paper-hdfs`。

## 常见问题

- **HDFS 提示安全模式**。检查 `dfsadmin -report` 和 `dfsadmin -safemode get`，确认 DataNode 注册和数据块上报情况。
- **DBLP 返回 HTML**。确认请求使用 `sparql.dblp.org/sparql`，并检查 HTTP 状态和代理设置。
- **本机 Elasticsearch 返回 HTTP 502**。如果终端开启了 HTTP 代理，将 `127.0.0.1` 和 `localhost` 加入 `NO_PROXY` 和 `no_proxy`，再检查本机端口。
- **Spark 能列文件但读不到正文**。检查 DataNode 的 `9866` 端口和 `dfs.client.use.datanode.hostname` 设置。
- **样例中出现论文集或 Workshop 介绍**。采集器按场所和年份取元数据。正式筛选时还需按课程采用的 CCF-A 目录与主会论文目录核对收录资格。
- **提取后的文字顺序有偏差**。双栏、表格和扫描页可能影响提取质量。检索页码采用 PDF 的物理页序。

## 参考

- [DBLP SPARQL 接口](https://blog.dblp.org/2024/09/09/introducing-our-public-sparql-query-service/)
- [OpenAlex PDF 链接和版本](https://help.openalex.org/data/locations/)
- [Hadoop HDFS 命令](https://hadoop.apache.org/docs/r3.5.0/hadoop-project-dist/hadoop-hdfs/HDFSCommands.html)
- [Spark JSON 数据源](https://spark.apache.org/docs/4.2.1/sql-data-sources-json.html)
- [Elasticsearch Bulk API](https://www.elastic.co/docs/api/doc/elasticsearch/operation/operation-bulk)

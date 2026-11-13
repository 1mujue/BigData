# 论文收集模块

按[仓库环境配置](../../README.md#环境配置)准备运行环境。以下命令从仓库根目录执行。采集与验证需要访问 DBLP SPARQL、OpenAlex 和公开 PDF 托管站点。

## 数据流程

| 阶段 | 输入与输出 | 工具 |
| --- | --- | --- |
| 元数据采集 | DBLP 会议或期刊标识与年份范围 → 每篇一行的 JSONL | Python、DBLP SPARQL |
| 入库和整理 | 已校验的 JSONL → HDFS 批次 → 去重后的 Parquet | Docker Compose、HDFS、PySpark |
| 查找和下载 | DOI → PDF 候选链接 → `<paper_id>.pdf` | OpenAlex、Python |
| 文本处理 | PDF → 每页一行的 JSONL → HDFS 页记录 | PyMuPDF、HDFS |
| 全文检索 | HDFS 页记录 → ES 页文档 → 页码和高亮 | PySpark、Elasticsearch |

`dblp_id` 是原始 DBLP 记录标识。`paper_id` 是该标识的 SHA-256，用来关联元数据、PDF 和逐页文本。Elasticsearch 的文档 ID 采用 `paper_id:page`，重跑同一篇同页数的 PDF 时会更新对应页文档。

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

## 一条命令验证整条流程

```bash
bash scripts/data_collect/workflows/verify_pipeline.sh
```

脚本依次检查以下内容。

1. DBLP 按每页 2 行分页采集 5 条元数据，并按 DOI `10.1145/3514221.3517851` 单独定位 Litmus。
2. 将两批 JSONL 上传到 HDFS，Spark 验证样例批次有 5 个唯一 ID，再生成合并后的 Parquet。
3. 从 OpenAlex 解析候选直链，用已核对的 CMU 公开副本下载 Litmus。这个固定备用地址使验证可以覆盖下载和 PDF 校验。
4. 提取 PDF 的 15 页正文，上传 PDF 和 JSONL，再核对 HDFS 读回的 PDF SHA-256。
5. Spark 从 HDFS 读回连续的 15 页，写入 `paper-pages-verification`，核对文档数并断言第 1 页返回短语高亮。

成功时末尾应出现类似输出。

```text
总页数 15，有文本的页数 15
索引 paper-pages-verification 已核对 15 页
命中页码 [1]
第 1 页高亮 ...
端到端验证通过。
```

每次运行都会打印独立的本地 `data/verification/run-…` 路径和 HDFS `/papers/verification/run-…` 路径，保留中间文件方便定位失败。验证索引按稳定 ID 更新样例页，可重复执行。样例的 DOI 和正式出版信息可查 MIT 机构库。 [MIT 论文记录](https://dspace.mit.edu/entities/publication/f005695a-2797-49b1-b068-08af75e54c73) · [CMU 公开 PDF](https://db.cs.cmu.edu/papers/2022/yu-litmusdb-sigmod2022.pdf)

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
- `--limit` 是整个查询去重后的篇数上限，默认为 20。记录按发表年份升序返回，取满后结束。`--all` 会分页取完整结果。`--page-size` 控制每次请求的行数，`--doi` 可进一步限定到一个 DOI。
- 输出是本地 JSONL，每行包含 DBLP 标识、标题、年份、发表场所标识和 DOI 链接。缺失 DOI 时该字段为 `null`。收集正式课程语料时，按课程采用的 CCF-A 目录和正式发表目录核对结果。

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

链接解析脚本目前取最多 10 篇带 DOI 的记录，状态分别为 `link_found`、`no_direct_pdf` 和 `no_match`。遇到 HTTP 403 时，可以为下载脚本传入已核对属于同一篇论文的公开备用链接 `--pdf-url`。下载成功后，用首页标题和 DOI 核对身份。

## 目录结构

| 路径 | 职责 |
| --- | --- |
| [__main__.py](__main__.py) | Python 命令入口，解析参数并调用对应模块。 |
| [metadata.py](metadata.py) | DBLP 元数据查询、Spark 元数据校验和 Parquet 表整理。 |
| [pdf.py](pdf.py) | OpenAlex 链接解析、PDF 下载和正文提取。 |
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

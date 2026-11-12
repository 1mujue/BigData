# 大数据课程作业 · 论文采集、处理与检索

这是一个运行在 WSL2 Ubuntu 上的单机课程项目。它按会议或期刊和发表年份采集论文元数据，将原始数据存入 HDFS，用 Spark 清洗和去重，再下载已选论文的公开 PDF，提取按页正文并写入 Elasticsearch。检索结果可以返回论文标识、PDF 页码和高亮片段。

## 当前进度

- **已实现** DBLP SPARQL 分页采集、按 DBLP ID 去重、HDFS 批次入库、Spark 校验和 Parquet 整理。
- **已实现** OpenAlex 公开 PDF 链接查询、指定论文下载、PDF 可读性检查、逐页提取和 Elasticsearch 全文检索。
- **课程样例** 使用 SIGMOD 2022 的 Litmus 论文验证整条流程。选定的 PDF 为 15 页，查询 `verifiable outsourced DBMS` 可以定位到第 1 页。
- **待完成** 确定正式采集范围，按课程采用的 CCF-A 目录和主会研究论文类型核对记录，扩大采集，提取复现实验条件并筛选候选论文。

## 数据如何流转

| 阶段 | 输入与输出 | 工具 |
| --- | --- | --- |
| 元数据采集 | DBLP 会议或期刊标识与年份 → 每篇一行的 JSONL | Python、DBLP SPARQL |
| 入库和整理 | 已校验的 JSONL → HDFS 批次 → 去重后的 Parquet | Docker Compose、HDFS、PySpark |
| 查找和下载 | DOI → PDF 候选链接 → `<paper_id>.pdf` | OpenAlex、Python |
| 文本处理 | PDF → 每页一行的 JSONL → HDFS 页记录 | PyMuPDF、HDFS |
| 全文检索 | HDFS 页记录 → ES 页文档 → 页码和高亮 | PySpark、Elasticsearch |

`dblp_id` 是原始 DBLP 记录标识。`paper_id` 是该标识的 SHA-256，用来关联元数据、PDF 和逐页文本。Elasticsearch 的文档 ID 采用 `paper_id:page`，重跑同一篇同页数的 PDF 时会更新对应页文档。

## 环境要求

在 **Ubuntu 终端**执行本文的 Bash 命令。

- WSL2 Ubuntu 22.04、可连接的 Docker Engine 和 Docker Compose。
- Conda 环境 `bigdata-course`，包含 Python 3.13、OpenJDK 17、PySpark 4.2.1、PyMuPDF 1.28.0。
- 可以访问 DBLP SPARQL、OpenAlex 和测试 PDF 的公开托管站点。Elasticsearch 和 HDFS 通过本机端口访问。

```bash
docker version
docker compose version

conda create -n bigdata-course --override-channels -c conda-forge \
  python=3.13 openjdk=17 pyspark=4.2.1 pymupdf=1.28.0
conda activate bigdata-course
python --version
"$JAVA_HOME/bin/java" -version
```

已有 `bigdata-course` 的组员可以直接激活环境并检查版本。Docker 中的 Hadoop 和 Elasticsearch 使用镜像自带的 Java。WSL 里的 Spark 使用 Conda 环境中的 Java。

## 启动 HDFS 和 Elasticsearch

从项目根目录运行。HDFS 由一个 NameNode 和一个 DataNode 组成，Elasticsearch 使用单节点 Docker 配置。各服务的数据保存在 Docker 命名卷中。

### 首次初始化空 HDFS 卷

以下两次 `chown` 将卷根目录交给镜像内的 `hadoop` 用户，`namenode -format` 在空卷上初始化元数据。**已有 HDFS 数据的环境直接使用下一节的启动命令。**

```bash
docker compose -f docker/compose.yaml config -q

docker compose -f docker/compose.yaml run --rm --no-deps \
  --user root --entrypoint chown namenode hadoop /data
docker compose -f docker/compose.yaml run --rm --no-deps \
  --user root --entrypoint chown datanode hadoop /data
docker compose -f docker/compose.yaml run --rm --no-deps \
  namenode hdfs namenode -format -nonInteractive
```

### 启动和检查服务

```bash
docker compose -f docker/compose.yaml up -d namenode datanode
docker compose -f docker/compose.yaml exec -T --interactive=false namenode \
  cat /proc/sys/vm/max_map_count
```

Elasticsearch 的 Docker 主机应满足 `vm.max_map_count >= 1048576`。若 Docker Engine 直接运行在 Ubuntu WSL 中，值偏低时在该发行版执行 `sudo sysctl -w vm.max_map_count=1048576`。若使用 Docker Desktop 的 WSL2 后端，在 Docker Desktop 使用的内核环境中按 Elastic 的官方说明调整。 [Elastic Docker 配置](https://www.elastic.co/docs/deploy-manage/deploy/self-managed/install-elasticsearch-docker-prod#set-vm-max-map-count)

```bash
docker compose -f docker/compose.yaml up -d elasticsearch
docker compose -f docker/compose.yaml ps

docker compose -f docker/compose.yaml exec -T --interactive=false namenode \
  hdfs dfsadmin -report

docker compose -f docker/compose.yaml exec -T --interactive=false namenode \
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

四个映射都绑定在 `127.0.0.1`。Elasticsearch 关闭认证以便本机课程实验。Docker 命名卷保存已有数据，日常可以用 `docker compose -f docker/compose.yaml stop` 停止服务，再用 `up -d` 启动。

## 一条命令验证整条流程

```bash
conda activate bigdata-course
bash scripts/verify_pipeline.sh
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

### 采集并保存元数据

```bash
python scripts/collect_dblp.py \
  --stream conf/sigmod --year 2022 --limit 25 --page-size 10 \
  --output data/raw/sigmod-2022.jsonl

bash scripts/ingest_metadata.sh conf/sigmod 2022 25 10
```

`--limit` 是去重后的记录上限。`--page-size` 控制每次请求的行数。`--doi` 可以限定到单篇已知 DOI。批次入库前由 Spark 校验行数、唯一 ID 和必填字段，入库文件以内容的 SHA-256 命名。同样内容的批次可重跑。

### 从多个批次生成论文表

首次由 WSL 里的 Spark 写入整理目录时，给运行用户准备写权限。

```bash
docker compose -f docker/compose.yaml exec -T --interactive=false namenode \
  hdfs dfs -mkdir -p /papers/metadata/curated
docker compose -f docker/compose.yaml exec -T --interactive=false namenode \
  hdfs dfs -chown "$(id -un)" /papers/metadata/curated

python scripts/build_metadata_table.py \
  hdfs://127.0.0.1:8020/papers/metadata/batches/conf/sigmod/2022 \
  hdfs://127.0.0.1:8020/papers/metadata/curated/sigmod-2022-v1
```

同一 `dblp_id` 优先保留带 DOI 的记录。再次生成时用新的 Parquet 路径，例如 `sigmod-2022-v2`。

### 查找公开链接、下载和索引

```bash
python scripts/resolve_pdf_links.py \
  hdfs://127.0.0.1:8020/papers/metadata/curated/sigmod-2022-v1 \
  data/processed/pdf-manifest.jsonl

paper_id='从清单复制已核对的 paper_id'
python scripts/download_pdfs.py \
  data/processed/pdf-manifest.jsonl data/raw/pdfs "$paper_id"

python scripts/extract_pdf_pages.py \
  "data/raw/pdfs/$paper_id.pdf" "data/processed/pages/$paper_id.jsonl"

docker compose -f docker/compose.yaml exec -T --interactive=false namenode \
  hdfs dfs -mkdir -p /papers/raw/pdfs /papers/processed/pages
docker compose -f docker/compose.yaml exec -T namenode \
  hdfs dfs -put -f - "/papers/raw/pdfs/$paper_id.pdf" < "data/raw/pdfs/$paper_id.pdf"
docker compose -f docker/compose.yaml exec -T namenode \
  hdfs dfs -put -f - "/papers/processed/pages/$paper_id.jsonl" \
  < "data/processed/pages/$paper_id.jsonl"

python scripts/index_paper_pages.py \
  "hdfs://127.0.0.1:8020/papers/processed/pages/$paper_id.jsonl" "$paper_id"
```

链接解析脚本目前取最多 10 篇带 DOI 的记录，状态分别为 `link_found`、`no_direct_pdf` 和 `no_match`。遇到 HTTP 403 时，可以为下载脚本传入已核对属于同一篇论文的公开备用链接 `--pdf-url`。下载成功后，用首页标题和 DOI 核对身份。

## 常见问题和当前边界

- **HDFS 写入提示安全模式** — 先查 `dfsadmin -report` 和 `dfsadmin -safemode get`。验证脚本读取一次状态后即返回错误，便于定位 DataNode 注册或块上报问题。
- **DBLP 返回 HTML** — 采集脚本使用 `sparql.dblp.org/sparql` 的公开接口。请求失败时检查 HTTP 状态、网络和代理设置。
- **Spark 无法读 HDFS** — 核对 `8020`、`9866` 的端口映射，以及 `dfs.client.use.datanode.hostname` 的配置。能连上 NameNode 后还需要连上 DataNode 才能读取正文。
- **正式收录口径** — 当前通用采集按发表场所和年份查 DBLP。正式建库前还要过滤整本论文集、Workshop 介绍等条目，并核对课程采用的 CCF-A 目录。
- **正文质量** — PDF 双栏、表格和扫描页可能影响文字提取顺序或覆盖率，索引页码使用 PDF 的物理页序。
- **仓库内容** — Git 收录代码、Compose 和文档。`data/` 下的下载文件与验证输出、Docker 数据卷和本地配置由各自的环境生成。

## 参考

- [DBLP SPARQL 接口](https://blog.dblp.org/2024/09/09/introducing-our-public-sparql-query-service/)
- [OpenAlex PDF 链接和版本](https://help.openalex.org/data/locations/)
- [Hadoop HDFS 命令](https://hadoop.apache.org/docs/r3.5.0/hadoop-project-dist/hadoop-hdfs/HDFSCommands.html)
- [Spark JSON 数据源](https://spark.apache.org/docs/4.2.1/sql-data-sources-json.html)
- [Elasticsearch Bulk API](https://www.elastic.co/docs/api/doc/elasticsearch/operation/operation-bulk)

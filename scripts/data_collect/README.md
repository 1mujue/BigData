# 论文采集、检索与粗筛

先按[仓库环境配置](../../README.md#环境配置)安装依赖，再[启动 HDFS 和 Elasticsearch](#启动-hdfs-和-elasticsearch)。以下命令均在仓库根目录执行。

## 准备候选语料

语料快照包含 `papers.jsonl` 和 `manifest.json`，数据文件不随 Git 分发。已有快照时，直接把目录传给 `build-corpus`。自行采集时，先在[来源配置](config/venues.json)中选择会议或期刊的官方目录、栏目和年度出版映射，再运行 `collect`。例如：

```bash
python -m scripts.data_collect collect \
  --config scripts/data_collect/config/venues.json --sources ACL ICLR ICML \
  --years 2024 2025 --output data/corpus/example
python -m scripts.data_collect build-corpus data/corpus/example
```

`--sources` 省略时使用所给配置中的全部来源；`--years` 指定年份，`--limit-per-source` 可对每个来源年份采样。使用 CVF 平台的会议以 `proceedings_url` 指定论文集，采集和 PDF 镜像共用这个地址；采用摘要页索引的镜像可配置 `abstract_index_url`。DBLP 等补充来源只为候选论文补充书目信息。缺 DOI、摘要或 PDF 均不影响进入候选库。

更新时用 `--previous 快照目录` 指定已有语料，并写入新的输出目录。完整采集会替换所选会议年份分区；采样保留未采到的论文，未请求的分区保持原样。论文由稳定的 `paper_id` 标识，补 DOI 不改变其身份。采集失败时查看清单的 `collection_status`、`partitions` 和论文的 `errors`；命令以非零状态退出。可用 `--refresh` 跳过采集缓存。

`build-corpus` 在 HDFS 的 `/papers/corpora/<snapshot_id>/` 保存快照，使用 Spark 校验行数、标识和必填字段，并生成 Parquet 表与覆盖统计。

## 建索引与粗筛

[筛选配置](config/screening.json)是一份可编辑的任务示例。建索引只需要其中的 `embedding`；自由搜索还可指定 BM25 字段权重 `fields` 和融合参数 `rrf_k`；批量粗筛使用 `topics` 与每主题选取数 `per_topic`。这些设置可以放在同一份 JSON 中。

```bash
python -m scripts.data_collect index-corpus data/corpus/example \
  --profile scripts/data_collect/config/screening.json
python -m scripts.data_collect screen \
  --profile scripts/data_collect/config/screening.json --output data/screening/example
```

嵌入输入为标题和摘要。服务地址、模型、维度与输入文本决定缓存键；相同文本可复用 `data/embeddings/cache.sqlite3` 中的向量。建立索引后，`papers-current` 默认指向该索引。试验其他快照可传 `--no-activate`，然后用输出中的具体索引名运行 `screen --index` 或 `search --index`。

Elasticsearch 对标题、摘要和关键词计算 BM25，并对检索范围内的文档向量计算精确余弦相似度。一个主题有多条查询描述时取最高余弦。应用按 RRF 合并两路排名：每路贡献 `1/(k+rank)`，未命中 BM25 的论文只贡献向量分数；并列时按向量相似度和论文 ID 排序。每个主题取前 N 篇，最后按论文 ID 合并。可用 `--venues`、`--years` 限定范围，用 `--per-topic N` 指定选取数量。

`scores.jsonl` 保存逐篇逐主题的分数和排名，`selected.jsonl` 保存入选论文及其主题依据。`configuration.json` 和 `summary.json` 记录运行配置、语料快照和运行标识。HDFS 保存运行文件和分数表；ES 的 `paper-screening-runs` 与 `paper-screening-results` 可用于查询已完成的运行和入选记录。

## 在全库或入选范围内搜索

```bash
python -m scripts.data_collect search --profile scripts/data_collect/config/screening.json \
  --text 'program repair using execution feedback' --top 20
python -m scripts.data_collect search --profile scripts/data_collect/config/screening.json \
  --text 'program repair using execution feedback' --run-id RUN_ID
```

`RUN_ID` 取自筛选结果的 `summary.json`。指定运行时，搜索使用其语料索引，仅对入选论文按新查询评分；筛选记录保持不变。查询嵌入模型须与索引一致。

## 入选论文 PDF

```bash
python -m scripts.data_collect collect-pdfs --selection data/screening/example
```

命令校验已完成运行的清单及哈希，只下载该运行入选的论文。默认从[来源配置](config/venues.json)查找平台入口，也可用 `--config` 指定其他配置。依次尝试官方来源、OpenAlex、Researchr 和 Firecrawl。用 `--max-searches` 限制 Firecrawl 新搜索次数，`--workers` 和 `--venue-workers` 调整下载与会议年份组并发。

本地日志保存在 `data/pdf_ingest/`，PDF 默认写入 HDFS 的 `/papers/selected/`。同一输出目录可续跑，也可接收另一份粗筛结果：已入库文件经核对后复用，文件保留首次准入的运行标识，各次清单存于 `runs/<run_id>/`。下载失败记录原因，不改变入选名单。

`extract-pages PDF OUTPUT` 提取物理页码和正文，`index-pages INPUT_URI PAPER_ID` 将页文本写入 ES，支持 `--es-url`。单篇下载诊断使用 `download-pdf`，独立书目查询使用 `query-bibliography`。运行 `python -m scripts.data_collect 命令名 --help` 查看参数。

## 凭据与网络

进程环境或仓库根目录的 `.env` 可设置 `EMBEDDING_API_KEY`、`FIRECRAWL_API_KEY` 和 `FIRECRAWL_BASE_URL`。还可传 `--env-file PATH` 补充未设置的变量。ES 服务启用 API 密钥认证时设置 `ELASTICSEARCH_API_KEY`。嵌入缓存未命中时需要嵌入服务密钥；Firecrawl 用于元数据和 PDF 的补充获取。外部请求使用配置的代理，本地服务建议在 `NO_PROXY` 中列出 `127.0.0.1,localhost`。

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
| `8020` | NameNode RPC，供本机 Spark 查询 HDFS |
| `9866` | DataNode 数据块传输 |
| `9870` | NameNode 管理页面 |
| `9200` | Elasticsearch HTTP API |

四个映射都绑定在 `127.0.0.1`。Elasticsearch 关闭认证以便本机课程实验。Docker 命名卷保存已有数据，日常可以用 `docker compose -f scripts/data_collect/docker/compose.yaml stop` 停止服务，再用 `up -d` 启动。

## 验证与开发入口

```bash
BIGDATA_ES_TESTS=1 python -m unittest discover -s tests -v
bash scripts/data_collect/workflows/verify_pipeline.sh \
  data/corpus/example scripts/data_collect/config/screening.json
```

端到端验证对指定快照建表和索引，用首个主题选取一篇，检查全库与入选范围搜索、PDF 入库、页文本和高亮。提供可用的嵌入服务或相应的缓存，并选择含可下载论文的样本。验证使用具体索引，不切换默认检索范围。建表会在指定快照目录记录 `storage.json`，语料按快照写入 HDFS；样本粗筛运行会登记在 ES 中。脚本可接受第三个参数指定环境文件。

| 修改内容 | 位置 |
| --- | --- |
| 官方目录、平台解析与 DBLP 补充 | [collectors/](collectors/) |
| 论文标识、语料快照和 Spark 表 | [corpus.py](corpus.py) |
| 向量、ES 论文索引、粗筛和检索 | [retrieval/](retrieval/) |
| PDF 来源、入库、续跑和页文本 | [pdfs/](pdfs/) |
| 来源和筛选任务配置 | [config/](config/) |

## 参考

- [ES dense_vector](https://www.elastic.co/docs/reference/elasticsearch/mapping-reference/dense-vector)
- [ES script_score 与余弦](https://www.elastic.co/docs/reference/query-languages/query-dsl/query-dsl-script-score-query)
- [DBLP SPARQL](https://blog.dblp.org/2024/09/09/introducing-our-public-sparql-query-service/)
- [OpenAlex PDF 链接和版本](https://help.openalex.org/data/locations/)
- [HDFS 命令](https://hadoop.apache.org/docs/r3.5.0/hadoop-project-dist/hadoop-hdfs/HDFSCommands.html)
- [Spark JSON 数据源](https://spark.apache.org/docs/4.2.1/sql-data-sources-json.html)

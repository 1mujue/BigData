# 大数据技术与实践课程项目

这是北京大学软件与微电子学院研一上学期《大数据技术与实践》课程的小组项目。我们先用大数据技术搜集、管理和筛选计算机领域的论文，再选定复现任务，比较 Agent 与人工独立复现的过程和结果。课程计划平均每人处理 10 篇论文。

## 项目阶段

| 阶段 | 工作 | 产出 |
| --- | --- | --- |
| 1. 论文收集与选择 | 按近 5 年正式发表于 CCF-A 期刊或会议的课程要求收集至少 500 篇论文。计算机科学子领域不限。 | 有出处的论文元数据与收录范围 |
| 2. 论文存储管理 | 使用 HDFS 存储元数据和原文，用 Spark 校验、去重和整理，用 Elasticsearch 检索正文。 | 可查询的论文库 |
| 3. 信息提取与论文筛选 | 评估核心实验、公开材料和资源需求，确定最终需要复现的论文。 | 最终论文清单与选择依据 |
| 4. 复现任务与评估设计 | 为每篇入选论文明确要复现的表格、图或结论，固定输入、基线、指标、资源预算和验收标准。 | Agent 与人工共用的任务说明和评估口径 |
| 5. 复现实施与结果对比 | Agent 和人工在相同的任务与评估条件下分别复现，记录各自的操作、耗时和结果。 | 双线复现记录、指标对比和差异分析 |

前三个阶段属于项目的前半阶段。进入后半阶段时，每篇入选论文应附上正式发表来源、原文链接、拟复现的结果、所需代码和数据，以及可用算力。两条复现路线分别记录所用环境、运行命令、遇到的问题和验收结果。

## 当前进度

- **采集与存储基础设施已通过样例验证。** 已跑通从论文元数据采集、PDF 下载到正文处理和检索的流程。
- **前半阶段待完成。** 确定正式采集范围，收集至少 500 篇论文，整理复现所需信息并完成选题。
- **后半阶段待开展。** 根据最终论文清单制定任务与评估标准，再实施 Agent 与人工双线复现。

## 环境配置

论文采集与处理流程已在 **WSL2 Ubuntu 22.04、Python 3.13、OpenJDK 17** 上验证。完整流程还需要 Docker Engine、Docker Compose 插件、Bash 和 `curl`。Windows 组员可在 WSL2 Ubuntu 终端中运行以下命令。

### 系统依赖

如果还未安装 Docker，可在 WSL Ubuntu 中安装 Docker Engine 和 Compose 插件，也可使用 Docker Desktop 并开启该发行版的 WSL 集成。安装后确认 Docker 客户端能连接到引擎。 [Docker Engine 安装](https://docs.docker.com/engine/install/ubuntu/) · [Docker Desktop 的 WSL 集成](https://docs.docker.com/desktop/features/wsl/)

在 Ubuntu 上安装 OpenJDK 17 和 `curl`，再检查 Docker 和 Compose。

```bash
sudo apt update
sudo apt install openjdk-17-jdk-headless curl
docker version
docker compose version
```

Spark 使用 `JAVA_HOME` 指向的 JDK。如果环境已经配置了 JDK 17，直接使用该环境。若刚通过 Ubuntu 安装 OpenJDK 17，可以在当前终端这样设置和检查。

```bash
export JAVA_HOME="$(dirname "$(dirname "$(dpkg -L openjdk-17-jdk-headless | grep '/bin/javac$')")")"
"$JAVA_HOME/bin/java" -version
```

### Python 依赖

激活自己选择的 Python 虚拟环境后，在仓库根目录运行。依赖清单适用于 Conda、venv 等环境。

```bash
python --version
python -m pip install -r requirements.txt
python -m pip check
```

`requirements.txt` 固定 PySpark 和 PyMuPDF 版本。仅从 DBLP 查询元数据时，可以直接用 Python 运行采集命令，该命令只用标准库。

## 仓库导航

- [论文收集模块](scripts/data_collect/README.md) 包含按期刊与年份查询、服务启动、数据处理和端到端验证。

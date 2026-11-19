# 大数据技术与实践课程项目

这是北京大学软件与微电子学院研一上学期《大数据技术与实践》课程的小组项目。我们先用大数据技术搜集、管理和筛选计算机领域的论文，再选定复现任务，比较 Agent 与人工独立复现的过程和结果。课程计划平均每人处理 10 篇论文。

## 项目阶段

| 阶段 | 工作 | 产出 |
| --- | --- | --- |
| 1. 论文收集与选择 | 按课程要求选择近 5 年正式发表于 CCF-A 期刊或会议的论文，采集规模按任务配置。计算机科学子领域不限。 | 有出处的论文元数据与收录范围 |
| 2. 论文存储管理 | 使用 HDFS 保存元数据和筛选结果，Spark 校验与统计，Elasticsearch 提供文本与向量检索；粗筛通过后再下载 PDF。 | 可查询的论文库 |
| 3. 信息提取与论文筛选 | 评估核心实验、公开材料和资源需求，确定最终需要复现的论文。 | 最终论文清单与选择依据 |
| 4. 复现任务与评估设计 | 为每篇入选论文明确要复现的表格、图或结论，固定输入、基线、指标、资源预算和验收标准。 | Agent 与人工共用的任务说明和评估口径 |
| 5. 复现实施与结果对比 | Agent 和人工在相同的任务与评估条件下分别复现，记录各自的操作、耗时和结果。 | 双线复现记录、指标对比和差异分析 |

前三个阶段属于项目的前半阶段。进入后半阶段时，每篇入选论文应附上正式发表来源、原文链接、拟复现的结果、所需代码和数据，以及可用算力。两条复现路线分别记录所用环境、运行命令、遇到的问题和验收结果。

## 论文库

[论文库使用说明](scripts/data_collect/README.md) 包含按官方目录采集论文、使用已有快照、在全库或入选范围内检索，以及入选论文 PDF 的下载和逐篇精筛。

## 环境配置

本项目使用 Python 3.13、OpenJDK 17、Docker Compose、Bash 和 `curl`。macOS 可使用本机 Python 和 Java；Windows 可在 WSL2 Ubuntu 中运行。以下系统安装命令以 Ubuntu 为例。

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

`requirements.txt` 固定 PySpark、PyMuPDF、NumPy、HTML 解析和环境变量加载的依赖版本。运行采集命令前安装该文件中的依赖。

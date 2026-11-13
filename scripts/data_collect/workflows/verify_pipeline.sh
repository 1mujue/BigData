#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../.."

compose=(docker compose -f scripts/data_collect/docker/compose.yaml)
hdfs() {
  "${compose[@]}" exec -T --interactive=false namenode hdfs "$@"
}
upload() {
  "${compose[@]}" exec -T namenode hdfs dfs -put - "$2" < "$1"
}

export PYTHONUNBUFFERED=1
python -c 'import pymupdf, pyspark'
curl --noproxy 127.0.0.1 --fail --silent --show-error --max-time 10 \
  http://127.0.0.1:9200/ >/dev/null
safe_mode=$(timeout 15s "${compose[@]}" exec -T --interactive=false \
  namenode hdfs dfsadmin -safemode get)
if [[ "$safe_mode" != *"Safe mode is OFF"* ]]; then
  printf '%s\n' "$safe_mode" "HDFS 尚不可写，请先检查 DataNode。" >&2
  exit 1
fi

mkdir -p data/verification
run_dir=$(mktemp -d data/verification/run-XXXXXXXX)
hdfs_run="/papers/verification/${run_dir##*/}"
hdfs_base="hdfs://127.0.0.1:8020"
doi=10.1145/3514221.3517851
pdf_url=https://db.cs.cmu.edu/papers/2022/yu-litmusdb-sigmod2022.pdf

printf '本地验证目录 %s\nHDFS 验证目录 %s\n' "$run_dir" "$hdfs_run"
printf '\n[1/5] 验证 DBLP 分页采集，另按 DOI 定位 Litmus\n'
python -m scripts.data_collect collect --stream conf/sigmod --year 2022 \
  --limit 5 --page-size 2 --output "$run_dir/sample.jsonl"
python -m scripts.data_collect collect --stream conf/sigmod --year 2022 \
  --doi "$doi" --limit 2 --output "$run_dir/litmus.jsonl"
paper_id=$(python - "$run_dir/litmus.jsonl" "$doi" <<'PY'
import hashlib
import json
import sys

with open(sys.argv[1], encoding="utf-8") as source:
    records = [json.loads(line) for line in source]

if (
    len(records) != 1
    or records[0]["doi_url"].lower().rsplit("doi.org/", 1)[-1] != sys.argv[2]
    or not records[0]["title"].startswith("Litmus:")
):
    raise SystemExit("DBLP 未按预期返回唯一的 Litmus 论文记录。")
print(hashlib.sha256(records[0]["dblp_id"].encode()).hexdigest())
PY
)

printf '\n[2/5] 上传元数据并由 Spark 校验，生成去重后的 Parquet\n'
hdfs dfs -mkdir -p "$hdfs_run/batches" "$hdfs_run/curated"
hdfs dfs -chown "$(id -un)" "$hdfs_run/curated"
upload "$run_dir/sample.jsonl" "$hdfs_run/batches/sample.jsonl"
upload "$run_dir/litmus.jsonl" "$hdfs_run/batches/litmus.jsonl"
python -m scripts.data_collect validate-metadata \
  "$hdfs_base$hdfs_run/batches/sample.jsonl" --expected-count 5
python -m scripts.data_collect build-metadata \
  "$hdfs_base$hdfs_run/batches" "$hdfs_base$hdfs_run/curated/papers"

printf '\n[3/5] 解析公开链接，使用已核对的 CMU 副本下载 Litmus\n'
manifest="$run_dir/pdf-manifest.jsonl"
python -m scripts.data_collect resolve-pdfs "$hdfs_base$hdfs_run/curated/papers" "$manifest"
python -m scripts.data_collect download-pdf "$manifest" "$run_dir/pdfs" "$paper_id" --pdf-url "$pdf_url"

printf '\n[4/5] 提取 PDF 页文本并上传，核对 HDFS 原始 PDF\n'
pdf_file="$run_dir/pdfs/$paper_id.pdf"
pages_file="$run_dir/$paper_id.jsonl"
python -m scripts.data_collect extract-pages "$pdf_file" "$pages_file"
upload "$pdf_file" "$hdfs_run/$paper_id.pdf"
upload "$pages_file" "$hdfs_run/$paper_id.jsonl"
local_hash=$(sha256sum < "$pdf_file")
remote_hash=$(hdfs dfs -cat "$hdfs_run/$paper_id.pdf" | sha256sum)
if [[ "$local_hash" != "$remote_hash" ]]; then
  printf 'HDFS PDF 的 SHA-256 与本地文件不同。\n' >&2
  exit 1
fi

printf '\n[5/5] 验证 15 条索引文档和第 1 页短语高亮\n'
python -m scripts.data_collect index-pages "$hdfs_base$hdfs_run/$paper_id.jsonl" "$paper_id" \
  --index paper-pages-verification --expected-pages 15 \
  --phrase 'verifiable outsourced DBMS' --expected-page 1
printf '\n端到端验证通过。\n'

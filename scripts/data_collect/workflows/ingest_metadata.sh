#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 3 || $# -gt 4 ]]; then
  echo "用法: bash scripts/data_collect/workflows/ingest_metadata.sh <stream> <year> <limit> [page-size]" >&2
  exit 2
fi

cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../.."

stream=$1
year=$2
limit=$3
page_size=${4:-100}

compose=(docker compose -f scripts/data_collect/docker/compose.yaml)
hdfs() {
  "${compose[@]}" exec -T --interactive=false namenode hdfs "$@"
}

work_dir=$(mktemp -d)
local_file="$work_dir/papers.jsonl"
staged_path=""

cleanup() {
  if [[ -n "$staged_path" ]]; then
    hdfs dfs -rm -f "$staged_path" >/dev/null 2>&1 || true
  fi
  rm -f -- "$local_file"
  rmdir -- "$work_dir"
}
trap cleanup EXIT

python -m scripts.data_collect collect \
  --stream "$stream" --year "$year" --limit "$limit" \
  --page-size "$page_size" --output "$local_file"

row_count=$(wc -l < "$local_file")
if (( row_count == 0 )); then
  echo "采集结果为空，请检查场所和年份参数。" >&2
  exit 1
fi

batch_hash=$(sha256sum "$local_file" | cut -d ' ' -f 1)
batch_dir="/papers/metadata/batches/$stream/$year"
batch_path="$batch_dir/$batch_hash.jsonl"
hdfs_base="hdfs://127.0.0.1:8020"

if hdfs dfs -test -e "$batch_path"; then
  echo "相同内容的批次已入库: $batch_path"
  exit 0
fi

staging_dir="/papers/metadata/staging"
hdfs dfs -mkdir -p "$staging_dir" "$batch_dir"
staged_path="$staging_dir/batch-${work_dir##*/}.jsonl"

"${compose[@]}" exec -T namenode hdfs dfs -put - "$staged_path" < "$local_file"
python -m scripts.data_collect validate-metadata "$hdfs_base$staged_path" \
  --expected-count "$row_count"

hdfs dfs -mv "$staged_path" "$batch_path"
staged_path=""
echo "已入库 $row_count 篇: $batch_path"

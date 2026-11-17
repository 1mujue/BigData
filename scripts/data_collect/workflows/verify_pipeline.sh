#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../.."
if [[ $# -lt 2 || $# -gt 3 ]]; then
  echo "Usage: bash scripts/data_collect/workflows/verify_pipeline.sh SNAPSHOT PROFILE [ENV_FILE]" >&2
  exit 2
fi
snapshot=$1
profile=$2
env_file=${3:-}
run_cli() {
  local args=("$@")
  if [[ -n "$env_file" ]]; then args+=(--env-file "$env_file"); fi
  python -m scripts.data_collect "${args[@]}"
}
compose=(docker compose -f scripts/data_collect/docker/compose.yaml)
hdfs() { "${compose[@]}" exec -T namenode hdfs dfs "$@"; }
export PYTHONUNBUFFERED=1
mkdir -p data/verification
run_dir=$(mktemp -d data/verification/run-XXXXXXXX)
hdfs_run="/papers/verification/${run_dir##*/}"
python - "$profile" "$run_dir" <<'PY'
import json,sys
from pathlib import Path
profile=json.loads(Path(sys.argv[1]).read_text())
profile['topics']=profile['topics'][:1]
profile['per_topic']=1
Path(sys.argv[2],'profile.json').write_text(json.dumps(profile,ensure_ascii=False,indent=2))
PY
printf 'Verification output: %s\n' "$run_dir"
run_cli build-corpus "$snapshot"
run_cli index-corpus "$snapshot" --profile "$profile" --no-activate > "$run_dir/index.json"
index=$(python -c 'import json,sys; print(json.load(open(sys.argv[1]))["index"])' "$run_dir/index.json")
run_cli screen --index "$index" --profile "$run_dir/profile.json" --output "$run_dir/screening" \
  --hdfs-root "$hdfs_run/screening"
run_id=$(python -c 'import json,sys; print(json.load(open(sys.argv[1]))["run_id"])' "$run_dir/screening/summary.json")
query=$(python -c 'import json,sys; print(json.load(open(sys.argv[1]))["topics"][0]["queries"][0])' "$profile")
run_cli search --index "$index" --profile "$profile" --text "$query" --top 3 --output "$run_dir/search-all.json"
run_cli search --index "$index" --profile "$profile" --text "$query" --run-id "$run_id" --output "$run_dir/search-selected.json"
run_cli collect-pdfs --selection "$run_dir/screening" \
  --output "$run_dir/pdf" --hdfs-root "$hdfs_run/selected"
python - "$run_dir" "$run_id" <<'PY'
import json,sys
from pathlib import Path
root=Path(sys.argv[1]); run=sys.argv[2]
selected=json.loads((root/'screening/selected.jsonl').read_text())
result=json.loads((root/'search-selected.json').read_text())
assert [r['paper_id'] for r in result['results']]==[selected['paper_id']]
manifest=[json.loads(line) for line in (root/'pdf/runs'/run/'manifest.jsonl').read_text().splitlines()]
assert len(manifest)==1 and manifest[0]['status']=='stored', 'Selected PDF unavailable; metadata/search checks passed, PDF check remains incomplete'
assert manifest[0]['first_screening_run']==run
(root/'pdf-record.json').write_text(json.dumps(manifest[0]))
PY
paper_id=$(python -c 'import json,sys; print(json.load(open(sys.argv[1]))["paper_id"])' "$run_dir/pdf-record.json")
pdf_path=$(python -c 'import json,sys; print(json.load(open(sys.argv[1]))["hdfs_path"])' "$run_dir/pdf-record.json")
hdfs -cat "$pdf_path" > "$run_dir/$paper_id.pdf"
python - "$run_dir/pdf-record.json" "$run_dir/$paper_id.pdf" <<'PY'
import hashlib,json,sys
from pathlib import Path
assert hashlib.sha256(Path(sys.argv[2]).read_bytes()).hexdigest()==json.load(open(sys.argv[1]))['sha256']
PY
run_cli extract-pages "$run_dir/$paper_id.pdf" "$run_dir/$paper_id.jsonl"
hdfs -mkdir -p "$hdfs_run/pages"
hdfs -put -f - "$hdfs_run/pages/$paper_id.jsonl" < "$run_dir/$paper_id.jsonl"
page_count=$(python -c 'import json,sys; print(json.load(open(sys.argv[1]))["pages"])' "$run_dir/pdf-record.json")
phrase=$(python -c 'import json,sys; print(" ".join(json.loads(open(sys.argv[1]).read())["title"].split()[:4]))' "$run_dir/screening/selected.jsonl")
page_index=$(printf 'paper-pages-%s' "${run_dir##*/}" | tr '[:upper:]' '[:lower:]')
hdfs_uri=$(python -c 'from scripts.data_collect.storage import HDFS_URI; print(HDFS_URI)')
run_cli index-pages "$hdfs_uri$hdfs_run/pages/$paper_id.jsonl" "$paper_id" \
  --index "$page_index" --expected-pages "$page_count" --phrase "$phrase" --expected-page 1
printf 'Pipeline verification completed: %s\n' "$run_dir"

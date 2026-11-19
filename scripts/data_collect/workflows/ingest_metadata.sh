#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../.."
if [[ $# -lt 3 ]]; then
  echo "Usage: bash scripts/data_collect/workflows/ingest_metadata.sh CONFIG YEAR OUTPUT [SOURCE ...]" >&2
  exit 2
fi
config=$1
year=$2
output=$3
shift 3
collect_args=(collect --config "$config" --years "$year" --output "$output")
if [[ $# -gt 0 ]]; then collect_args+=(--sources "$@"); fi
python -m scripts.data_collect "${collect_args[@]}"
python -m scripts.data_collect build-corpus "$output"

import argparse
import json
import re
from collections import Counter
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from pyspark.sql import SparkSession
from pyspark.sql import functions as F

parser = argparse.ArgumentParser(description="为一小批论文查询公开 PDF 链接")
parser.add_argument("input_uri", help="HDFS 中的 Parquet 目录")
parser.add_argument("output", type=Path, help="输出 JSONL 清单")
args = parser.parse_args()

doi_prefix = r"(?i)^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)"

spark = (
    SparkSession.builder
    .master("local[2]")
    .appName("resolve-paper-pdf-links")
    .config("spark.hadoop.dfs.client.use.datanode.hostname", "true")
    .getOrCreate()
)

try:
    papers = (
        spark.read.parquet(args.input_uri)
        .withColumn("doi", F.lower(F.regexp_replace(F.trim("doi_url"), doi_prefix, "")))
        .where(F.col("doi").startswith("10."))
        .select("paper_id", "title", "doi")
        .orderBy("paper_id")
        .limit(10)
        .collect()
    )
finally:
    spark.stop()

if not papers:
    raise SystemExit("这批数据没有可用于查询的 DOI。")

dois = sorted({paper.doi for paper in papers})
params = urlencode({
    "filter": "doi:" + "|".join(dois),
    "select": "doi,best_oa_location,locations",
    "per_page": 100,
})
request = Request(
    f"https://api.openalex.org/works?{params}",
    headers={"Accept": "application/json"},
)

with urlopen(request, timeout=30) as response:
    works = json.load(response)["results"]

works_by_doi = {}
for work in works:
    locations = work.get("locations") or []
    for doi in [work.get("doi"), *(location.get("doi") for location in locations)]:
        if doi:
            works_by_doi[re.sub(doi_prefix, "", doi.strip()).lower()] = work

manifest = []
for paper in papers:
    work = works_by_doi.get(paper.doi)
    locations = [] if work is None else [
        work.get("best_oa_location") or {},
        *(work.get("locations") or []),
    ]
    direct_pdf = next(
        (location for location in locations if location.get("is_oa") and location.get("pdf_url")),
        {},
    )

    status = "no_match" if work is None else "no_direct_pdf"
    if direct_pdf:
        status = "link_found"

    manifest.append({
        **paper.asDict(),
        "pdf_url": direct_pdf.get("pdf_url"),
        "pdf_version": direct_pdf.get("version"),
        "status": status,
    })

args.output.parent.mkdir(parents=True, exist_ok=True)
with args.output.open("w", encoding="utf-8") as output:
    for record in manifest:
        print(json.dumps(record, ensure_ascii=False), file=output)

print(dict(Counter(record["status"] for record in manifest)))
print(f"清单已保存到 {args.output}")
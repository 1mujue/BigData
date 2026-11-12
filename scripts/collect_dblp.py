import json
import argparse
import re
from tempfile import TemporaryDirectory
from pathlib import Path
from urllib.request import Request, urlopen

parser = argparse.ArgumentParser(description="从DBLP采集论文元数据")
parser.add_argument("--stream",required=True, help="会议流名称，例如 conf/sigmod")
parser.add_argument("--year", required=True, type=int, help="年份，例如 2022")
parser.add_argument("--doi", help="可选的原始 DOI，例如 10.1145/3514221.3517851")
parser.add_argument("--limit",type=int,default=20, help="限制返回的记录数，默认 20")
parser.add_argument("--output", required=True, type=Path, help="输出 JSONL 路径")
parser.add_argument("--page-size",type=int,default=100, help="每次请求最大行数")
args = parser.parse_args()
if not re.fullmatch(r"(?:conf|journals)/[A-Za-z0-9][A-Za-z0-9_.-]*", args.stream):
    parser.error("--stream 应为 conf/简称 或 journals/简称")
if args.limit < 1:
    parser.error("--limit 必须是正整数")
if args.page_size < 1:
    parser.error("--page-size 必须是正整数")

def fetch_page(page_limit, offset):
    doi_filter = ""
    if args.doi:
        doi = json.dumps(args.doi.strip().lower())
        doi_filter = (
            'FILTER(REPLACE(LCASE(STR(?doi)), "^https?://(dx[.])?doi[.]org/", "") = '
            + doi + ')'
        )
    query = f"""
PREFIX dblp: <https://dblp.org/rdf/schema#>

SELECT DISTINCT ?paper ?title ?year ?doi WHERE {{
?paper dblp:publishedInStream <https://dblp.org/streams/{args.stream}> ;
        dblp:title ?title ;
        dblp:yearOfPublication ?year .
FILTER(STR(?year) = "{args.year}")
OPTIONAL {{ ?paper dblp:doi ?doi . }}
{doi_filter}
}}
ORDER BY ?paper ?title ?year ?doi
LIMIT {page_limit}
OFFSET {offset}
"""

    request = Request(
        "https://sparql.dblp.org/sparql",
        data=query.encode("utf-8"),
        headers={
            "Content-Type": "application/sparql-query",
            "Accept": "application/sparql-results+json",
        },
    )

    with urlopen(request, timeout=30) as response:
        return json.load(response)["results"]["bindings"]

records = {}
offset = 0

while len(records) < args.limit:
    page_limit = min(args.page_size,args.limit-len(records))
    rows=fetch_page(page_limit,offset)
    for row in rows:
        dblp_id = row["paper"]["value"]
        records.setdefault(dblp_id, {
            "dblp_id": dblp_id,
            "title": row["title"]["value"],
            "year": int(row["year"]["value"]),
            "venue_stream": args.stream,
            "doi_url": row.get("doi", {}).get("value"),
        })

    print(f"偏移量 {offset}，返回 {len(rows)} 行，累计 {len(records)} 篇")
    offset += len(rows)

    if len(rows) < page_limit:
        break

output_path = args.output
output_path.parent.mkdir(parents=True, exist_ok=True)

with TemporaryDirectory(dir=output_path.parent) as temp_dir:
    temp_path = Path(temp_dir) / output_path.name
    with temp_path.open("w", encoding="utf-8") as output:
        for record in records.values():
            print(json.dumps(record, ensure_ascii=False), file=output)
    temp_path.replace(output_path)

print(f"取得 {len(records)} 篇不同论文，保存到 {output_path}")
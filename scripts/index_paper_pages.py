import argparse
import json
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

from pyspark.sql import SparkSession

ES_BASE = "http://127.0.0.1:9200"
opener = build_opener(ProxyHandler({}))


def es_request(method, path, body, content_type="application/json"):
    data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
    request = Request(
        ES_BASE + path,
        data=data,
        method=method,
        headers={"Content-Type": content_type},
    )
    with opener.open(request, timeout=30) as response:
        return json.load(response)


def read_pages(input_uri):
    spark = (
        SparkSession.builder.master("local[2]")
        .appName("index-paper-pages")
        .config("spark.hadoop.dfs.client.use.datanode.hostname", "true")
        .getOrCreate()
    )
    try:
        return (
            spark.read.schema("paper_id STRING, page INT, text STRING")
            .option("mode", "FAILFAST")
            .json(input_uri)
            .orderBy("page")
            .collect()
        )
    finally:
        spark.stop()


def ensure_index(index_path):
    settings = {
        "settings": {"number_of_replicas": 0},
        "mappings": {"properties": {
            "paper_id": {"type": "keyword"},
            "page": {"type": "integer"},
            "text": {"type": "text"},
        }},
    }
    try:
        es_request("PUT", index_path, settings)
    except HTTPError as exc:
        if exc.code != 400 or json.load(exc)["error"]["type"] != "resource_already_exists_exception":
            raise


def main():
    parser = argparse.ArgumentParser(description="将一篇论文的 HDFS 页记录写入 Elasticsearch")
    parser.add_argument("input_uri")
    parser.add_argument("paper_id")
    parser.add_argument("--index", default="paper-pages")
    parser.add_argument("--expected-pages", type=int)
    parser.add_argument("--phrase")
    parser.add_argument("--expected-page", type=int)
    args = parser.parse_args()
    if (args.phrase is None) != (args.expected_page is None):
        parser.error("--phrase 和 --expected-page 需要一起使用。")

    pages = read_pages(args.input_uri)
    if not pages or any(page.paper_id != args.paper_id or page.text is None for page in pages):
        raise SystemExit("页记录为空，或 paper_id、正文格式不符合预期。")
    if [page.page for page in pages] != list(range(1, len(pages) + 1)):
        raise SystemExit("页码必须从 1 开始连续排列。")
    if args.expected_pages is not None and len(pages) != args.expected_pages:
        raise SystemExit(f"读到 {len(pages)} 页，预期 {args.expected_pages} 页。")

    index_path = f"/{args.index}"
    ensure_index(index_path)
    lines = []
    for page in pages:
        lines.append(json.dumps({"index": {"_id": f"{page.paper_id}:{page.page}"}}))
        lines.append(json.dumps(page.asDict(), ensure_ascii=False))
    payload = ("\n".join(lines) + "\n").encode("utf-8")
    result = es_request(
        "POST", f"{index_path}/_bulk?refresh=wait_for", payload, "application/x-ndjson"
    )
    if result["errors"] or len(result["items"]) != len(pages):
        errors = [
            item["index"]["error"] for item in result["items"] if "error" in item["index"]
        ]
        raise SystemExit(f"Elasticsearch 批量写入失败 {errors[:3]}")

    paper_filter = {"term": {"paper_id": args.paper_id}}
    indexed_count = es_request("POST", f"{index_path}/_count", {"query": paper_filter})["count"]
    if indexed_count != len(pages):
        raise SystemExit(f"索引有 {indexed_count} 页，输入有 {len(pages)} 页。")
    print(f"索引 {args.index} 已核对 {indexed_count} 页")

    if args.phrase is not None:
        query = {
            "size": len(pages),
            "_source": ["paper_id", "page"],
            "query": {"bool": {
                "filter": [paper_filter],
                "must": [{"match_phrase": {"text": args.phrase}}],
            }},
            "highlight": {"fields": {"text": {"number_of_fragments": 1}}},
        }
        hits = es_request("POST", f"{index_path}/_search", query)["hits"]["hits"]
        matching_pages = sorted(hit["_source"]["page"] for hit in hits)
        for hit in hits:
            if hit["_source"]["page"] == args.expected_page and hit.get("highlight", {}).get("text"):
                print(f"命中页码 {matching_pages}")
                print(f"第 {args.expected_page} 页高亮 {hit['highlight']['text'][0]}")
                break
        else:
            raise SystemExit(f"短语未在预期第 {args.expected_page} 页返回高亮。")


if __name__ == "__main__":
    main()

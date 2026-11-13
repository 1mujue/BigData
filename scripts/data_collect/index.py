import json
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

from .tool import spark_session


class PaperPageIndex:
    def __init__(self, name, base_url="http://127.0.0.1:9200"):
        self.base_url = base_url
        self.index_path = f"/{name}"
        self.opener = build_opener(ProxyHandler({}))

    def index_pages(self, paper_id, pages):
        self._ensure_index()
        lines = []
        for page in pages:
            lines.append(json.dumps({"index": {"_id": f"{page.paper_id}:{page.page}"}}))
            lines.append(json.dumps(page.asDict(), ensure_ascii=False))
        payload = ("\n".join(lines) + "\n").encode("utf-8")
        result = self._request(
            "POST", "/_bulk?refresh=wait_for", payload, "application/x-ndjson"
        )
        if result["errors"] or len(result["items"]) != len(pages):
            errors = [
                item["index"]["error"] for item in result["items"] if "error" in item["index"]
            ]
            raise SystemExit(f"Elasticsearch 批量写入失败 {errors[:3]}")

        paper_filter = {"term": {"paper_id": paper_id}}
        indexed_count = self._request("POST", "/_count", {"query": paper_filter})["count"]
        if indexed_count != len(pages):
            raise SystemExit(f"索引有 {indexed_count} 页，输入有 {len(pages)} 页。")
        return indexed_count

    def verify_phrase(self, paper_id, page_count, phrase, expected_page):
        paper_filter = {"term": {"paper_id": paper_id}}
        query = {
            "size": page_count,
            "_source": ["paper_id", "page"],
            "query": {"bool": {
                "filter": [paper_filter],
                "must": [{"match_phrase": {"text": phrase}}],
            }},
            "highlight": {"fields": {"text": {"number_of_fragments": 1}}},
        }
        hits = self._request("POST", "/_search", query)["hits"]["hits"]
        matching_pages = sorted(hit["_source"]["page"] for hit in hits)
        for hit in hits:
            if hit["_source"]["page"] == expected_page and hit.get("highlight", {}).get("text"):
                print(f"命中页码 {matching_pages}")
                print(f"第 {expected_page} 页高亮 {hit['highlight']['text'][0]}")
                break
        else:
            raise SystemExit(f"短语未在预期第 {expected_page} 页返回高亮。")

    def _ensure_index(self):
        settings = {
            "settings": {"number_of_replicas": 0},
            "mappings": {"properties": {
                "paper_id": {"type": "keyword"},
                "page": {"type": "integer"},
                "text": {"type": "text"},
            }},
        }
        try:
            self._request("PUT", "", settings)
        except HTTPError as exc:
            if exc.code != 400:
                raise
            if json.load(exc)["error"]["type"] != "resource_already_exists_exception":
                raise

    def _request(self, method, path, body, content_type="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        request = Request(
            self.base_url + self.index_path + path,
            data=data,
            method=method,
            headers={"Content-Type": content_type},
        )
        with self.opener.open(request, timeout=30) as response:
            return json.load(response)


def read_pages(input_uri):
    with spark_session("index-paper-pages") as spark:
        return (
            spark.read.schema("paper_id STRING, page INT, text STRING")
            .option("mode", "FAILFAST")
            .json(input_uri)
            .orderBy("page")
            .collect()
        )

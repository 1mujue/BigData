"""Small Elasticsearch HTTP client used by indexing, screening and search."""
import json
import os
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener


class ElasticsearchError(RuntimeError):
    def __init__(self, status, reason):
        self.status = status
        self.reason = reason
        super().__init__(f"Elasticsearch HTTP {status}: {reason}")


class Elasticsearch:
    def __init__(self, base_url="http://127.0.0.1:9200"):
        self.base_url = base_url.rstrip("/")
        local = urlsplit(base_url).hostname in {"localhost", "127.0.0.1", "::1"}
        self.opener = build_opener(ProxyHandler({})) if local else build_opener()

    def request(self, method, path, body=None):
        headers = {"Content-Type": "application/json"}
        if os.environ.get("ELASTICSEARCH_API_KEY"):
            headers["Authorization"] = "ApiKey " + os.environ["ELASTICSEARCH_API_KEY"]
        if isinstance(body, str):
            data = body.encode()
            headers["Content-Type"] = "application/x-ndjson"
        else:
            data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
        try:
            with self.opener.open(Request(self.base_url + path, data=data, method=method, headers=headers), timeout=180) as response:
                result = json.load(response)
        except HTTPError as error:
            value = json.loads(error.read())
            raise ElasticsearchError(error.code, value.get("error", value)) from None
        if isinstance(result, dict) and (result.get("timed_out") or result.get("_shards", {}).get("failed")):
            raise RuntimeError(f"Incomplete Elasticsearch response: {result.get('_shards')}")
        return result

    def exists(self, index):
        try:
            self.request("GET", f"/{index}/_mapping")
            return True
        except ElasticsearchError as error:
            if error.status == 404:
                return False
            raise

    def bulk(self, index, records, *, batch_size=100):
        lines = []
        count = 0
        def send():
            response = self.request("POST", "/_bulk", "\n".join(lines) + "\n")
            if response["errors"]:
                failures = [item["index"].get("error") for item in response["items"] if item["index"].get("error")]
                raise RuntimeError(f"Elasticsearch bulk indexing failed: {failures[:3]}")
        for identifier, record in records:
            lines.extend((json.dumps({"index": {"_index": index, "_id": identifier}}), json.dumps(record, ensure_ascii=False)))
            count += 1
            if count % batch_size == 0:
                send()
                lines.clear()
        if lines:
            send()
        self.request("POST", f"/{index}/_refresh")
        return count

    def scan(self, index, query, *, source=False):
        """Scroll an exact-scoring batch without repeatedly sorting the full corpus."""
        page = self.request("POST", f"/{index}/_search?scroll=5m", {
            "size": 1000, "sort": ["_doc"], "track_scores": True, "track_total_hits": True,
            "_source": source, "query": query,
        })
        scroll = page.get("_scroll_id")
        expected = page["hits"]["total"]["value"]
        received = 0
        try:
            while page["hits"]["hits"]:
                for hit in page["hits"]["hits"]:
                    received += 1
                    yield hit
                page = self.request("POST", "/_search/scroll", {"scroll": "5m", "scroll_id": scroll})
                scroll = page.get("_scroll_id", scroll)
            if received != expected:
                raise RuntimeError(f"Incomplete scan: {received}/{expected}")
        finally:
            if scroll:
                self.request("DELETE", "/_search/scroll", {"scroll_id": [scroll]})

    def corpus(self, name):
        mapping = self.request("GET", f"/{name}/_mapping")
        if len(mapping) != 1:
            raise ValueError("Select exactly one corpus index/snapshot")
        index, value = next(iter(mapping.items()))
        meta = value["mappings"].get("_meta", {})
        if meta.get("kind") != "paper-corpus" or not meta.get("ready"):
            raise ValueError("Index is not a completed paper corpus")
        return index, meta

    def documents(self, index, ids):
        records = []
        for start in range(0, len(ids), 200):
            result = self.request("POST", f"/{index}/_mget?_source_excludes=embedding", {"ids": ids[start:start + 200]})
            for document in result["docs"]:
                if not document.get("found"):
                    raise ValueError(f"Corpus document unavailable: {document.get('_id')}")
                records.append(document["_source"])
        return records

    def point_alias(self, alias, index):
        try:
            previous = self.request("GET", f"/_alias/{alias}")
        except ElasticsearchError as error:
            if error.status != 404:
                raise
            previous = {}
        actions = [{"remove": {"index": name, "alias": alias}} for name in previous]
        actions.append({"add": {"index": index, "alias": alias}})
        self.request("POST", "/_aliases", {"actions": actions})

from ..retrieval.elastic import Elasticsearch, ElasticsearchError
from ..tool import spark_session, write_jsonl


def read_pdf_pages(source, *, sort=False):
    import pymupdf

    try:
        document = pymupdf.open(stream=source, filetype="pdf") if isinstance(source, bytes) else pymupdf.open(source)
        with document:
            if not document.is_pdf or document.needs_pass:
                raise ValueError("输入文件必须是可读的 PDF。")
            pages = [page.get_text("text", sort=sort) for page in document]
    except (pymupdf.FileDataError, RuntimeError) as error:
        raise ValueError("输入文件必须是可读的 PDF。") from error
    if not any(page.strip() for page in pages):
        raise ValueError("未提取到正文，请检查 PDF 文本层。")
    return pages


class PdfPageExtractor:
    def __init__(self, pdf_path):
        self.pdf_path = pdf_path

    def extract(self, output_path):
        pages = read_pdf_pages(self.pdf_path, sort=True)
        write_jsonl(output_path, (
            {"paper_id": self.pdf_path.stem, "page": number, "text": text.strip()}
            for number, text in enumerate(pages, 1)
        ))
        return len(pages), sum(bool(text.strip()) for text in pages)


class PaperPageIndex:
    def __init__(self, name, base_url="http://127.0.0.1:9200"):
        self.es = Elasticsearch(base_url)
        self.name = name

    def index_pages(self, paper_id, pages):
        self._ensure_index()
        self.es.bulk(self.name, ((f"{page.paper_id}:{page.page}", page.asDict()) for page in pages))
        paper_filter = {"term": {"paper_id": paper_id}}
        indexed_count = self.es.request("POST", f"/{self.name}/_count", {"query": paper_filter})["count"]
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
        hits = self.es.request("POST", f"/{self.name}/_search", query)["hits"]["hits"]
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
            self.es.request("PUT", f"/{self.name}", settings)
        except ElasticsearchError as exc:
            if (exc.status != 400 or not isinstance(exc.reason, dict)
                    or exc.reason.get("type") != "resource_already_exists_exception"):
                raise


def read_pages(input_uri):
    with spark_session("index-paper-pages") as spark:
        return (
            spark.read.schema("paper_id STRING, page INT, text STRING")
            .option("mode", "FAILFAST")
            .json(input_uri)
            .orderBy("page")
            .collect()
        )

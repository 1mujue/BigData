import json
import re
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from .tool import write_jsonl


DOI_PREFIX = r"(?i)^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)"


class OpenAlexPdfResolver:
    def resolve(self, papers):
        works = self._lookup_works({paper.doi for paper in papers})
        manifest = []
        for paper in papers:
            work = works.get(paper.doi)
            locations = []
            if work is not None:
                locations = [work.get("best_oa_location") or {}, *(work.get("locations") or [])]
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
        return manifest

    def _lookup_works(self, dois):
        params = urlencode({
            "filter": "doi:" + "|".join(sorted(dois)),
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
                    works_by_doi[re.sub(DOI_PREFIX, "", doi.strip()).lower()] = work
        return works_by_doi


class PdfDownloader:
    def __init__(self, output_dir):
        self.output_dir = output_dir

    def download(self, paper_id, url):
        import pymupdf

        address = urlsplit(url)
        if address.scheme not in {"http", "https"} or not address.hostname:
            raise ValueError("PDF 链接必须是有效的 HTTP 或 HTTPS 地址。")

        self.output_dir.mkdir(parents=True, exist_ok=True)
        destination = self.output_dir / f"{paper_id}.pdf"
        with TemporaryDirectory(dir=self.output_dir) as temp_dir:
            temp_file = Path(temp_dir) / "download.pdf"
            try:
                with urlopen(url, timeout=30) as response, temp_file.open("wb") as output:
                    shutil.copyfileobj(response, output)
            except HTTPError as exc:
                host = urlsplit(exc.geturl()).hostname
                raise ValueError(f"下载失败：HTTP {exc.code}，站点 {host}") from None

            try:
                with pymupdf.open(temp_file) as document:
                    if not document.is_pdf or document.needs_pass or document.page_count < 1:
                        raise ValueError("下载内容无法作为可读的 PDF 使用。")
                    page_count = document.page_count
            except pymupdf.FileDataError as exc:
                raise ValueError("下载内容无法作为可读的 PDF 使用。") from exc
            temp_file.replace(destination)
        return destination, page_count


class PdfPageExtractor:
    def __init__(self, pdf_path):
        self.pdf_path = pdf_path

    def extract(self, output_path):
        import pymupdf

        pages_with_text = 0
        with pymupdf.open(self.pdf_path) as document:
            if not document.is_pdf or document.needs_pass:
                raise ValueError("输入文件必须是可读的 PDF。")

            def page_records():
                nonlocal pages_with_text
                for page_number, page in enumerate(document, start=1):
                    text = page.get_text("text", sort=True).strip()
                    pages_with_text += bool(text)
                    yield {"paper_id": self.pdf_path.stem, "page": page_number, "text": text}

                if not pages_with_text:
                    raise ValueError("未提取到正文，请检查 PDF 文本层。")

            write_jsonl(output_path, page_records())
            return document.page_count, pages_with_text

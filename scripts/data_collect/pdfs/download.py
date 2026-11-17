import hashlib
import re
import threading
from collections import deque
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlencode, urljoin, urlsplit

from ..tool import write_jsonl
from ..network import FetchError, HttpClient
from .candidates import candidate, canonical_url, figshare_article_id, html_candidates, identity_matches, normalized


PDF_LOCK = threading.Lock()


DOI_PREFIX = r"(?i)^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)"


class OpenAlexPdfResolver:
    def __init__(self, client=None):
        self.client = client or HttpClient()

    def resolve(self, papers):
        dois = {paper["paper_id"]: re.sub(DOI_PREFIX, "", (paper.get("doi") or "").strip()).lower()
                for paper in papers}
        works = self._lookup_works({doi for doi in dois.values() if doi})
        by_paper = {}
        for paper_id, doi in dois.items():
            work = works.get(doi)
            locations = []
            if work is not None:
                locations = [work.get("best_oa_location") or {}, *(work.get("locations") or [])]
            candidates = []
            seen = set()
            for field in ("pdf_url", "landing_page_url"):
                for location in locations:
                    url = location.get(field)
                    if location.get("is_oa") and url and url not in seen:
                        seen.add(url)
                        candidates.append({"url": url, "source": "openalex", "trusted": True,
                                           "version": location.get("version")})
            by_paper[paper_id] = candidates
        return by_paper

    def _lookup_works(self, dois):
        works_by_doi = {}
        dois = sorted(dois)
        for start in range(0, len(dois), 50):
            params = urlencode({"filter": "doi:" + "|".join(dois[start:start + 50]),
                                "select": "doi,best_oa_location,locations", "per_page": 100})
            works = self.client.json(f"https://api.openalex.org/works?{params}")["results"]
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
        paper = {"paper_id": paper_id, "title": "", "authors": []}
        result = PdfResolver(HttpClient(), self.output_dir).download(
            paper, [candidate(url, "manual")]
        )
        if result["status"] != "downloaded":
            errors = [attempt["error"] for attempt in result["attempts"] if attempt.get("error")]
            raise ValueError(errors[-1] if errors else "未找到可读的 PDF。")
        return Path(result["local_path"]), result["pages"]


class PdfResolver:
    def __init__(self, client, output_dir):
        self.client = client
        self.output_dir = Path(output_dir)

    def download(self, paper, candidates):
        import pymupdf

        self.output_dir.mkdir(parents=True, exist_ok=True)
        result = {"paper_id": paper["paper_id"], "status": "missing", "attempts": []}
        seen = set()
        queue = deque((entry, 0) for entry in candidates)
        with TemporaryDirectory(dir=self.output_dir) as directory:
            path = Path(directory) / "response"
            while queue and len(seen) < 12:
                entry, depth = queue.popleft()
                url = canonical_url(entry["url"])
                if url in seen or urlsplit(url).scheme not in {"http", "https"}:
                    continue
                seen.add(url)
                attempt = {"url": url, "source": entry["source"]}
                result["attempts"].append(attempt)
                try:
                    final_url = self.client.fetch(url, path)
                    with path.open("rb") as source:
                        is_pdf = source.read(1024).lstrip().startswith(b"%PDF-")
                    if is_pdf:
                        with PDF_LOCK, pymupdf.open(path) as document:
                            if not document.is_pdf or document.needs_pass or document.page_count == 0:
                                raise ValueError("Unreadable PDF")
                            pages = document.page_count
                            if not entry["trusted"]:
                                text = "\n".join(document[i].get_text() for i in range(min(2, pages)))
                                if normalized(paper["title"]) not in normalized(text) or not any(
                                    normalized(author) in normalized(text) for author in paper.get("authors", [])):
                                    raise ValueError("PDF identity not established")
                        destination = self.output_dir / f'{paper["paper_id"]}.pdf'
                        with path.open("rb") as source:
                            digest = hashlib.file_digest(source, "sha256").hexdigest()
                        size = path.stat().st_size
                        path.replace(destination)
                        attempt["status"] = "pdf"
                        return {**result, "status": "downloaded", "local_path": str(destination),
                                "source_url": final_url, "source": entry["source"], "version": entry.get("version"),
                                "sha256": digest, "bytes": size, "pages": pages}
                    attempt["status"] = "not_pdf"
                    if depth >= 2:
                        continue
                    html = path.read_text(encoding="utf-8", errors="replace")
                    soup, urls = html_candidates(html, final_url)
                    primary_urls = {urljoin(final_url, tag["content"]) for tag in soup.select('meta[name="citation_pdf_url"][content]')}
                    titles = [tag["content"] for tag in soup.select('meta[name="citation_title"][content]')]
                    authors = [tag["content"] for tag in soup.select('meta[name="citation_author"][content]')]
                    trusted = entry["trusted"] or any(identity_matches(paper, title, authors) for title in titles)
                    # DSpace's public REST endpoint serves bytes when the UI download route serves HTML.
                    match = re.search(r"/bitstreams/([0-9a-f-]+)/download", final_url)
                    if match:
                        content_url = f'{urlsplit(final_url).scheme}://{urlsplit(final_url).netloc}/server/api/core/bitstreams/{match[1]}/content'
                        urls.insert(0, content_url)
                        primary_urls.add(content_url)
                    # Figshare-backed institutional repositories share the public article API.
                    article_id = figshare_article_id(soup, final_url)
                    if article_id:
                        try:
                            article = self.client.json("https://api.figshare.com/v2/articles/" + article_id)
                            if identity_matches(paper, article.get("title", ""), [a.get("full_name", "") for a in article.get("authors", [])]):
                                trusted = True
                                files = [file["download_url"] for file in article.get("files", []) if file.get("name", "").lower().endswith(".pdf")]
                                urls.extend(files)
                                primary_urls.update(files)
                        except (FetchError, ValueError, OSError) as error:
                            attempt["error"] = f"Figshare lookup failed: {error}"
                    for link in reversed(urls[:8]):
                        # Generic page anchors may point to referenced papers or supplementary files.
                        queue.appendleft(({**entry, "url": link, "trusted": trusted and link in primary_urls}, depth + 1))
                except (FetchError, ValueError, OSError, pymupdf.FileDataError) as error:
                    attempt["status"] = "failed"
                    attempt["error"] = str(error)
        return result


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

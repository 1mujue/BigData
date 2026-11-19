"""PDF candidates from shared publishing platforms, independent of paper identity."""
import json
import os
import re
import unicodedata
from collections import defaultdict
from urllib.parse import urlencode, urljoin, urlsplit

from bs4 import BeautifulSoup

def normalized(value):
    return "".join(c for c in unicodedata.normalize("NFKD", value).casefold() if c.isalnum())


def candidate(url, source, *, trusted=True, version=None):
    return {"url": url, "source": source, "trusted": trusted, "version": version}


def canonical_url(url):
    parsed = urlsplit(url)
    if parsed.hostname in {"doi.org", "dx.doi.org"} and parsed.path.startswith("/10.1145/"):
        return "https://dl.acm.org/doi/pdf" + parsed.path
    if parsed.hostname in {"arxiv.org", "www.arxiv.org", "export.arxiv.org"}:
        return "https://arxiv.org" + parsed.path.replace("/abs/", "/pdf/")
    if parsed.hostname == "github.com" and "/blob/" in parsed.path and parsed.path.endswith(".pdf"):
        return "https://raw.githubusercontent.com" + parsed.path.replace("/blob/", "/")
    return url


def identity_matches(paper, title, authors):
    if normalized(paper["title"]) != normalized(title):
        return False
    return bool({normalized(a) for a in paper.get("authors", [])} & {normalized(a) for a in authors})


def html_candidates(html, url):
    soup = BeautifulSoup(html, "html.parser")
    links = [urljoin(url, tag["content"]) for tag in soup.select('meta[name="citation_pdf_url"][content]')]
    for tag in soup.select("a[href], iframe[src], embed[src], object[data]"):
        label = tag.get_text(" ", strip=True).lower()
        if any(word in label for word in ("supplement", "slides", "poster", "appendix", "bibtex")):
            continue
        href = urljoin(url, tag.get("href") or tag.get("src") or tag.get("data"))
        path = urlsplit(href).path.lower()
        if path.endswith(".pdf") or "/article/download/" in path or label in {"pdf", "download pdf", "full text", "paper", "pre-print", "preprint"}:
            links.append(href)
    return soup, list(dict.fromkeys(links))


def figshare_article_id(soup, url):
    # Institutional domains use the same platform assets as figshare.com.
    hosts = [urlsplit(url).hostname, *(urlsplit(urljoin(url, tag["src"])).hostname
                                     for tag in soup.select("script[src]"))]
    if not any(host and (host == "figshare.com" or host.endswith(".figshare.com")) for host in hosts):
        return None
    match = re.search(r"/articles/(?:[^/]+/){1,2}(\d+)(?:/|$)", urlsplit(url).path)
    return match[1] if match else None


class SourceCatalog:
    def __init__(self, client, config):
        self.client = client
        self.config = config
        self.indexes = {}
        self.researchr_pages = {}

    def official_index(self, venue, year):
        key = (venue, year)
        if key in self.indexes:
            return self.indexes[key]
        index = defaultdict(list)
        settings = self.config.get(venue, {})
        if settings.get("adapter") == "cvf":
            template = settings["proceedings_url"]
            selector = "dt.ptitle a[href]"
        elif template := settings.get("abstract_index_url"):
            selector = 'a[href*="Abstract"]'
        else:
            return index

        url = template.format(venue=venue, year=year)
        soup = BeautifulSoup(self.client.text(url, cache=True), "html.parser")
        for anchor in soup.select(selector):
            index[normalized(anchor.get_text(" ", strip=True))].append(urljoin(url, anchor["href"]))
        if not index:
            raise ValueError(f"Empty official index: {url}")
        self.indexes[key] = index
        return index

    def direct(self, paper):
        index = self.indexes.get((paper["venue"], paper["year"]), {})
        matches = index.get(normalized(paper["title"]), [])
        urls = matches if len(matches) == 1 else []
        urls = [*urls, *(paper.get("pdf_urls") or []), *(paper.get("document_urls") or [])]
        if paper.get("paper_url"):
            urls.append(paper["paper_url"])
        if paper.get("doi"):
            urls.append("https://doi.org/" + paper["doi"])
        if paper.get("doi_url"):
            urls.append(paper["doi_url"])
        # Try public archives before publisher redirects and OpenReview fallbacks.
        urls.sort(key=lambda url: urlsplit(url).hostname in {"doi.org", "dl.acm.org", "ieeexplore.ieee.org", "openreview.net"})
        return [candidate(url, "official") for url in dict.fromkeys(urls)]

    def prepare_researchr(self, venue, year):
        key = (venue, year)
        if key in self.researchr_pages:
            return
        url = self.config[venue].get("researchr", {}).get(str(year))
        if not url:
            self.researchr_pages[key] = None
            return
        soup = BeautifulSoup(self.client.text(url, cache=True), "html.parser")
        section = soup.select_one("#event-overview")
        form = soup.select_one("#event-modal-loader form")
        if section is None or form is None:
            raise ValueError(f"Researchr paper table or form missing: {venue} {year}")
        params = {node["name"]: node.get("value", "") for node in form.select("input[name]")}
        params[form.select_one("[submitid]")["submitid"]] = "1"
        params["__ajax_runtime_request__"] = "event-modal-loader"
        index = defaultdict(list)
        for row in section.select("tr"):
            anchor = row.select_one("a[data-event-modal]")
            if anchor:
                for badge in anchor.select(".output-badge"):
                    badge.decompose()
                index[normalized(anchor.get_text(" ", strip=True))].append((anchor["data-event-modal"], str(row)))
        self.researchr_pages[key] = (url, urljoin(url, form["action"]), params,
                                    form.select_one(".event-id-input")["name"], index)

    def researchr(self, paper):
        page = self.researchr_pages.get((paper["venue"], paper["year"]))
        if page is None:
            return []
        url, endpoint, params, field, index = page
        matches = index.get(normalized(paper["title"]), [])
        if len(matches) != 1:
            return []
        event, row_html = matches[0]
        data = self.client.json(endpoint, data=urlencode({**params, field: event}),
                                headers={"Content-Type": "application/x-www-form-urlencoded"})
        bodies = [item.get("value", "") for item in data
                  if item.get("action") == "append" and item.get("id") == "event-modals"]
        if not bodies:
            raise ValueError("Researchr returned no paper detail")
        soup = BeautifulSoup(row_html + "".join(bodies), "html.parser")
        urls = []
        for anchor in soup.select(".publication-link[href], .modal-footer a[href]"):
            label = anchor.get_text(" ", strip=True).lower()
            if any(word in label for word in ("slide", "poster", "presentation")):
                continue
            link = urljoin(url, anchor["href"])
            if "/details/" in link:
                detail = BeautifulSoup(self.client.text(link, cache=True), "html.parser")
                # Later program sections refer to other papers; use this paper's attachment fields only.
                for tag in detail.find_all("label"):
                    if tag.get_text(" ", strip=True) in {"Link to Publication", "Pre-print", "DOI", "File attachments"}:
                        if "presentation" not in tag.parent.get_text(" ", strip=True).lower():
                            urls.extend(urljoin(link, a["href"]) for a in tag.parent.select("a[href]"))
            elif "doi.org/10.48550/arxiv." in link.lower():
                urls.append("https://arxiv.org/pdf/" + link.rsplit("arXiv.", 1)[-1])
            elif any(part in link for part in (".pdf", "arxiv.org", "doi.org", "dl.acm.org/doi")) or "pre" in label or "publication" in label:
                urls.append(link)
        return [candidate(url, "researchr") for url in dict.fromkeys(urls)]


class FirecrawlSearch:
    def __init__(self, client):
        self.client = client
        self.key = os.environ.get("FIRECRAWL_API_KEY")
        self.base = os.environ.get("FIRECRAWL_BASE_URL", "https://api.firecrawl.dev/v2").rstrip("/")
        self.cache_dir = client.cache_dir / "search" if client.cache_dir else None

    def cached(self, paper):
        return bool(self.cache_dir and (self.cache_dir / (paper["paper_id"] + ".json")).exists())

    def search(self, paper):
        cache_file = self.cache_dir / (paper["paper_id"] + ".json") if self.cache_dir else None
        if cache_file and cache_file.exists():
            return json.loads(cache_file.read_text())
        if not self.key:
            raise ValueError("FIRECRAWL_API_KEY is not configured")
        author = (paper.get("authors") or [""])[0]
        result = self.client.json(self.base + "/search", data=json.dumps({
            "query": f'"{paper["title"].rstrip(".")}" {author} pdf',
            "sources": [{"type": "web"}], "limit": 5,
        }), headers={"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"})
        if result.get("success") is False:
            raise ValueError("Firecrawl search failed")
        data = result.get("data", {})
        results = data if isinstance(data, list) else data.get("web", [])
        candidates = [candidate(row["url"], "firecrawl", trusted=False) for row in results if row.get("url")]
        if cache_file:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(json.dumps(candidates), encoding="utf-8")
        return candidates

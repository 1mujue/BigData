"""Metadata HTTP/cache access using the project's transport and configured Firecrawl."""
import gzip
import hashlib
import json
import os
import threading
from pathlib import Path
from urllib.parse import quote, urlencode

from bs4 import BeautifulSoup

from ..network import FetchError, HttpClient
from .model import now


class Fetcher:
    def __init__(self, cache, refresh=False):
        self.cache = Path(cache)
        self.cache.mkdir(parents=True, exist_ok=True)
        self.refresh = refresh
        self.client = HttpClient(timeout=90)
        self.remote = bool(os.environ.get("FIRECRAWL_API_KEY"))
        self.counts = {"cached": 0, "requested": 0, "remote": 0}
        self.lock = threading.Lock()

    def _path(self, key):
        return self.cache / (hashlib.sha256(key.encode()).hexdigest() + ".json.gz")

    def _cached(self, key):
        path = self._path(key)
        if self.refresh or not path.exists():
            return None
        with gzip.open(path, "rt", encoding="utf-8") as source:
            value = json.load(source)
        with self.lock:
            self.counts["cached"] += 1
        return value["body"]

    def _save(self, key, body, provider, metadata=None):
        path = self._path(key)
        temporary = path.with_name(path.name + f".{threading.get_ident()}.tmp")
        with gzip.open(temporary, "wt", encoding="utf-8") as output:
            json.dump({"url": key, "fetched_at": now(), "provider": provider,
                       "body": body, "page_metadata": metadata or {}}, output, ensure_ascii=False)
        temporary.replace(path)
        return body

    @staticmethod
    def _readable(body, url):
        if any(marker in body for marker in ("anubis_version", "ChallengeRequiredError", "<title>Just a moment")):
            raise FetchError(f"Public metadata access blocked: {url}")
        if not body.strip():
            raise FetchError(f"Empty metadata response: {url}")
        return body

    def get(self, url):
        cached = self._cached(url)
        if cached is not None:
            return self._readable(cached, url)
        with self.lock:
            self.counts["requested"] += 1
        body = self._readable(self.client.text(url), url)
        return self._save(url, body, "http")

    def _scrape(self, url):
        key = os.environ.get("FIRECRAWL_API_KEY")
        if not key:
            raise FetchError("FIRECRAWL_API_KEY is required for the configured remote fetch")
        base = os.environ.get("FIRECRAWL_BASE_URL", "https://api.firecrawl.dev/v2").rstrip("/")
        result = self.client.json(base + "/scrape", data=json.dumps({
            "url": url, "formats": ["rawHtml", "markdown"], "onlyMainContent": False,
        }), headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
        if result.get("success") is not True:
            raise FetchError(f"Remote metadata fetch failed: {url}")
        data = result["data"]
        status = data.get("metadata", {}).get("statusCode", 200)
        if status and status >= 400:
            raise FetchError(url, f"HTTP {status}")
        with self.lock:
            self.counts["remote"] += 1
        return data

    def html(self, url, force_remote=False):
        if not force_remote:
            try:
                return self.get(url)
            except FetchError:
                if not self.remote:
                    raise
        key = url + "#remote-html"
        cached = self._cached(key)
        if cached is not None:
            return self._readable(cached, url)
        data = self._scrape(url)
        body = self._readable(data.get("rawHtml") or data.get("html") or "", url)
        return self._save(key, body, "firecrawl", data.get("metadata"))

    def json(self, url):
        cached = self._cached(url + "#remote-json")
        if cached is not None:
            return json.loads(cached)
        try:
            return json.loads(self.get(url))
        except (FetchError, json.JSONDecodeError) as error:
            if not self.remote or (isinstance(error, FetchError) and error.status == "HTTP 404"):
                raise
        data = self._scrape(url)
        body = (data.get("markdown") or "").strip()
        if body.startswith("```"):
            body = body.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        try:
            result = json.loads(body)
        except json.JSONDecodeError:
            body = BeautifulSoup(data.get("rawHtml", ""), "html.parser").get_text().strip()
            try:
                result = json.loads(body)
            except json.JSONDecodeError:
                raise FetchError(f"Remote fetch did not return JSON: {url}") from None
        self._save(url + "#remote-json", body, "firecrawl")
        return result

    def page_metadata(self, url):
        path = self._path(url + "#remote-html")
        if not path.exists():
            return {}
        with gzip.open(path, "rt", encoding="utf-8") as source:
            return json.load(source).get("page_metadata", {})

    def read_form(self, url, params, cache_key):
        cached = self._cached(cache_key)
        if cached is not None:
            return cached
        body = self.client.text(url, data=urlencode(params), headers={"Content-Type": "application/x-www-form-urlencoded"})
        json.loads(body)
        return self._save(cache_key, body, "public-detail-form")

    def github(self, repo, path, ref="main"):
        path = quote(path, safe="/")
        body = self.get(f"https://raw.githubusercontent.com/{repo}/{ref}/{path}")
        if body.startswith("version https://git-lfs.github.com/spec/v1"):
            body = self.get(f"https://media.githubusercontent.com/media/{repo}/{ref}/{path}")
        return body

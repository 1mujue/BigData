"""Small curl transport shared by collection and PDF downloads."""
import hashlib
import json
import sys
import subprocess
import threading
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit


class FetchError(RuntimeError):
    def __init__(self, host, status=None):
        self.status = status
        super().__init__(f"{host}: {status}" if status is not None else host)


class HttpClient:
    def __init__(self, cache_dir=None, *, timeout=600):
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.timeout = timeout
        self._guard = threading.Lock()
        self._hosts = {}

    def _host_state(self, host):
        with self._guard:
            return self._hosts.setdefault(host, {"lock": threading.Lock(),
                "slots": threading.BoundedSemaphore(4), "last": 0})

    def fetch(self, url, path, *, data=None, headers=None):
        address = urlsplit(url)
        if address.scheme not in {"http", "https"} or not address.hostname:
            raise ValueError("Expected an HTTP(S) URL")
        host = address.hostname
        state = self._host_state(host)
        with state["slots"], TemporaryDirectory() as directory:
            headers_path = Path(directory) / "headers"
            interval = 15 if host.endswith("arxiv.org") else 0.3
            for attempt in range(3):
                # Space request starts without holding the lock during file transfer.
                with state["lock"]:
                    time.sleep(max(0, state["last"] + interval - time.monotonic()))
                    state["last"] = time.monotonic()
                def quoted(value):
                    return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n').replace('\r', '\\r') + '"'
                config = ["url = " + quoted(url)]
                config.extend("header = " + quoted(f"{key}: {value}") for key, value in (headers or {}).items())
                if data is not None:
                    config.append("data = " + quoted(data))
                # Headers, including credentials, travel on stdin, never in argv or logs.
                result = subprocess.run(
                    ["curl", "--config", "-", "--silent", "--show-error", "--location",
                     "--connect-timeout", "15", "--max-time", str(self.timeout), "--user-agent", "Mozilla/5.0",
                     "--speed-limit", "1024", "--speed-time", "90", "--max-redirs", "8",
                     "--dump-header", str(headers_path),
                     "--output", str(path), "--write-out", "%{http_code}\n%{url_effective}"],
                    input="\n".join(config), text=True, capture_output=True,
                )
                code, _, final_url = result.stdout.partition("\n")
                status = int(code) if code.isdigit() else 0
                response_host = urlsplit(final_url).hostname or host
                if result.returncode == 0 and 200 <= status < 300:
                    return final_url
                transient = result.returncode in {5, 6, 7, 18, 28, 35, 52, 56} or status in {408, 429, 500, 502, 503, 504}
                if transient and attempt < 2:
                    delay = 2 ** (attempt + 1)
                    if status == 429:
                        for line in headers_path.read_text(encoding="latin-1").splitlines():
                            name, _, value = line.partition(":")
                            if name.lower() == "retry-after" and value.strip().isdigit():
                                delay = max(delay, int(value.strip()))
                        print(f"{response_host}: HTTP 429, retrying in {delay}s", file=sys.stderr, flush=True)
                    time.sleep(delay)
                    continue
                raise FetchError(response_host, f"HTTP {status}" if status else f"curl {result.returncode}")

    def text(self, url, *, data=None, headers=None, cache=False):
        cache_file = None
        if cache and self.cache_dir and data is None:
            cache_file = self.cache_dir / (hashlib.sha256(url.encode()).hexdigest() + ".html")
            if cache_file.exists():
                return cache_file.read_text(encoding="utf-8")
        with TemporaryDirectory() as directory:
            path = Path(directory) / "response"
            self.fetch(url, path, data=data, headers=headers)
            content = path.read_text(encoding="utf-8", errors="replace")
        if cache_file:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(content, encoding="utf-8")
        return content

    def json(self, url, *, data=None, headers=None):
        return json.loads(self.text(url, data=data, headers=headers))

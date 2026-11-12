import argparse
import json
import re
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit
from urllib.request import urlopen
from urllib.error import HTTPError

import pymupdf

parser = argparse.ArgumentParser(description="下载清单中指定论文的 PDF")
parser.add_argument("manifest", type=Path)
parser.add_argument("output_dir", type=Path)
parser.add_argument("paper_id")
parser.add_argument("--pdf-url", help="已核对属于同一篇论文的备用 PDF 直链")
args = parser.parse_args()

if not re.fullmatch(r"[0-9a-f]{64}", args.paper_id):
    parser.error("paper_id 应为清单中的 64 位十六进制字符串。")

with args.manifest.open(encoding="utf-8") as source:
    records = {record["paper_id"]: record for record in map(json.loads, source)}

paper = records.get(args.paper_id)
if paper is None:
    raise SystemExit("清单里找不到该 paper_id。")

pdf_url = args.pdf_url or paper.get("pdf_url")
if not pdf_url:
    raise SystemExit("清单里没有 PDF 直链，可以用 --pdf-url 提供已核对的链接。")

address = urlsplit(pdf_url)
if address.scheme not in {"http", "https"} or not address.hostname:
    raise SystemExit("PDF 链接必须是有效的 HTTP 或 HTTPS 地址。")

args.output_dir.mkdir(parents=True, exist_ok=True)
destination = args.output_dir / f"{args.paper_id}.pdf"

with TemporaryDirectory(dir=args.output_dir) as temp_dir:
    temp_file = Path(temp_dir) / "download.pdf"
    try:
        with urlopen(pdf_url, timeout=30) as response, temp_file.open("wb") as output:
            shutil.copyfileobj(response, output)
    except HTTPError as exc:
        host = urlsplit(exc.geturl()).hostname
        raise SystemExit(f"下载失败：HTTP {exc.code}，站点 {host}") from None

    with pymupdf.open(temp_file) as document:
        if not document.is_pdf or document.needs_pass or document.page_count < 1:
            raise SystemExit("下载内容无法作为可读的 PDF 使用。")
        page_count = document.page_count

    temp_file.replace(destination)

print(f"下载成功，共 {page_count} 页，下载入口 {address.hostname}")
print(f"保存到 {destination}")
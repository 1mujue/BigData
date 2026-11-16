import argparse
import json
import re
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit

from .index import PaperPageIndex, read_pages
from .metadata import DblpCollector, MetadataTable
from .pdf import OpenAlexPdfResolver, PdfDownloader, PdfPageExtractor
from .tool import spark_session, write_jsonl


def collect(args, parser):
    if (args.start_year is None) != (args.end_year is None):
        parser.error("--start-year 和 --end-year 必须一起使用")
    first_year = args.year if args.year is not None else args.start_year
    try:
        collector = DblpCollector(args.stream, first_year, args.doi, end_year=args.end_year)
        records = collector.collect(None if args.all else args.limit, args.page_size)
    except ValueError as error:
        parser.error(str(error))

    write_jsonl(args.output, records)
    print(f"取得 {len(records)} 篇不同论文，保存到 {args.output}")


def validate_metadata(args, _parser):
    with spark_session("validate-paper-metadata") as spark:
        MetadataTable(spark).validate(args.hdfs_uri, args.expected_count)


def build_metadata(args, _parser):
    with spark_session("build-paper-metadata-table") as spark:
        MetadataTable(spark).build(args.input_uri, args.output_uri)


def collect_pdfs(args, parser):
    from .batch import collect_and_download

    if args.workers < 1 or (args.max_searches is not None and args.max_searches < 0):
        parser.error("--workers 必须大于 0，--max-searches 不能小于 0")
    collect_and_download(args)


def resolve_pdfs(args, _parser):
    with spark_session("resolve-paper-pdf-links") as spark:
        papers = spark.read.parquet(args.input_uri).orderBy("paper_id")
        if args.limit:
            papers = papers.limit(args.limit)
        papers = papers.collect()

    if not papers:
        raise SystemExit("这批元数据为空。")

    manifest = OpenAlexPdfResolver().resolve(papers)
    write_jsonl(args.output, manifest)
    print(dict(Counter(record["status"] for record in manifest)))
    print(f"清单已保存到 {args.output}")


def download_pdf(args, parser):
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

    try:
        destination, page_count = PdfDownloader(args.output_dir).download(args.paper_id, pdf_url)
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    print(f"下载成功，共 {page_count} 页，下载入口 {urlsplit(pdf_url).hostname}")
    print(f"保存到 {destination}")


def extract_pages(args, _parser):
    try:
        page_count, pages_with_text = PdfPageExtractor(args.pdf).extract(args.output)
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    print(f"总页数 {page_count}，有文本的页数 {pages_with_text}")
    print(f"输出文件 {args.output}")


def index_pages(args, parser):
    if (args.phrase is None) != (args.expected_page is None):
        parser.error("--phrase 和 --expected-page 需要一起使用。")

    pages = read_pages(args.input_uri)
    if not pages or any(page.paper_id != args.paper_id or page.text is None for page in pages):
        raise SystemExit("页记录为空，或 paper_id、正文格式不符合预期。")
    if [page.page for page in pages] != list(range(1, len(pages) + 1)):
        raise SystemExit("页码必须从 1 开始连续排列。")
    if args.expected_pages is not None and len(pages) != args.expected_pages:
        raise SystemExit(f"读到 {len(pages)} 页，预期 {args.expected_pages} 页。")

    index = PaperPageIndex(args.index)
    indexed_count = index.index_pages(args.paper_id, pages)
    print(f"索引 {args.index} 已核对 {indexed_count} 页")
    if args.phrase is not None:
        index.verify_phrase(args.paper_id, len(pages), args.phrase, args.expected_page)


def main():
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
    root_parser = argparse.ArgumentParser(
        prog="python -m scripts.data_collect",
        description="论文采集与处理",
    )
    commands = root_parser.add_subparsers(dest="command", required=True)

    parser = commands.add_parser("collect", help="从 DBLP 采集论文元数据")
    parser.set_defaults(handler=collect, command_parser=parser)
    parser.add_argument("--stream", required=True, help="例如 conf/sigmod 或 journals/tkde")
    years = parser.add_mutually_exclusive_group(required=True)
    years.add_argument("--year", type=int, help="单个发表年份")
    years.add_argument("--start-year", type=int, help="范围起始年份，包含该年")
    parser.add_argument("--end-year", type=int, help="范围结束年份，包含该年")
    parser.add_argument("--doi", help="可选的原始 DOI，例如 10.1145/3514221.3517851")
    result_limit = parser.add_mutually_exclusive_group()
    result_limit.add_argument("--limit", type=int, default=20, help="去重后的总篇数上限（默认 20）")
    result_limit.add_argument("--all", action="store_true", help="采集范围内的全部记录")
    parser.add_argument("--output", required=True, type=Path, help="输出 JSONL 路径")
    parser.add_argument("--page-size", type=int, default=100, help="每次请求的最大行数")

    parser = commands.add_parser("validate-metadata", help="校验 HDFS 中的论文元数据")
    parser.set_defaults(handler=validate_metadata, command_parser=parser)
    parser.add_argument("hdfs_uri", help="输入 JSONL 的 HDFS URI")
    parser.add_argument("--expected-count", required=True, type=int)

    parser = commands.add_parser("build-metadata", help="生成去重后的论文元数据表")
    parser.set_defaults(handler=build_metadata, command_parser=parser)
    parser.add_argument("input_uri", help="已校验的 JSONL 批次目录")
    parser.add_argument("output_uri", help="本次生成的 Parquet 目录")

    parser = commands.add_parser("resolve-pdfs", help="分批查询 OpenAlex 的全部公开 PDF 候选")
    parser.set_defaults(handler=resolve_pdfs, command_parser=parser)
    parser.add_argument("input_uri", help="HDFS 中的 Parquet 目录")
    parser.add_argument("output", type=Path, help="输出 JSONL 清单")
    parser.add_argument("--limit", type=int, help="可选的论文数上限")

    parser = commands.add_parser("collect-pdfs", help="按会议、年份自动采集、下载 PDF 并入库 HDFS，可续跑")
    parser.set_defaults(handler=collect_pdfs, command_parser=parser)
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("venues.json"))
    parser.add_argument("--venues", nargs="+", help="默认采集配置中的全部会议和期刊")
    parser.add_argument("--years", nargs="+", type=int, default=[2024, 2025, 2026])
    parser.add_argument("--output", type=Path, default=Path("data/collection"))
    parser.add_argument("--hdfs-root", default="/papers/collection/main")
    parser.add_argument("--limit-per-venue", type=int, help="验证用：每个会议、年份的论文上限；默认全量")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--max-searches", type=int, help="可选的 Firecrawl 新查询总数上限；默认不限，0 为不新增查询")

    parser = commands.add_parser("download-pdf", help="下载清单中指定论文的 PDF")
    parser.set_defaults(handler=download_pdf, command_parser=parser)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("paper_id")
    parser.add_argument("--pdf-url", help="已核对属于同一篇论文的备用 PDF 直链")

    parser = commands.add_parser("extract-pages", help="按页提取论文 PDF 正文")
    parser.set_defaults(handler=extract_pages, command_parser=parser)
    parser.add_argument("pdf", type=Path)
    parser.add_argument("output", type=Path)

    parser = commands.add_parser("index-pages", help="将一篇论文的 HDFS 页记录写入 Elasticsearch")
    parser.set_defaults(handler=index_pages, command_parser=parser)
    parser.add_argument("input_uri")
    parser.add_argument("paper_id")
    parser.add_argument("--index", default="paper-pages")
    parser.add_argument("--expected-pages", type=int)
    parser.add_argument("--phrase")
    parser.add_argument("--expected-page", type=int)

    args = root_parser.parse_args()
    args.handler(args, args.command_parser)


if __name__ == "__main__":
    main()

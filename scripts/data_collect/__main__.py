import argparse
import json
import re
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

from .fine_screening import cli as fine_screening_cli
from .pdfs.pages import PaperPageIndex, PdfPageExtractor, read_pages
from .collectors.dblp import DblpCollector
from .pdfs.download import PdfDownloader
from .tool import write_json, write_jsonl


HERE = Path(__file__).resolve().parent


def collect(args, parser):
    from .collectors import collect_sources
    return collect_sources(args)


def build_corpus(args, parser):
    from .corpus import build_corpus as build
    return build(args.snapshot, args.hdfs_root)


def index_corpus(args, parser):
    from .retrieval.screening import index_corpus as index, load_profile
    return index(args.snapshot, load_profile(args.profile, args.command), args.cache, es_url=args.es_url, hdfs_base=args.hdfs_root, activate=not args.no_activate)


def screen(args, parser):
    from .retrieval.screening import screen as run, load_profile
    return run(args.index, load_profile(args.profile, args.command), args.cache, args.output,
               es_url=args.es_url, per_topic=args.per_topic, venues=args.venues,
               years=args.years, hdfs_base=args.hdfs_root)


def search(args, parser):
    from .retrieval.screening import search as query, load_profile
    if args.top < 1:
        parser.error("--top must be positive")
    result = query(args.index, args.text, load_profile(args.profile, args.command), args.cache,
                   es_url=args.es_url, run_id=args.run_id, venues=args.venues, years=args.years, top=args.top)
    if args.output:
        write_json(args.output, result)
        return {"results": len(result["results"]), "output": str(args.output.resolve()), "scope_run": args.run_id}
    return {**result, "results": [{k: r[k] for k in ("paper_id", "title", "venue", "year", "paper_url", "search_scores") if k in r}
                                  for r in result["results"]]}


def collect_pdfs(args, parser):
    from .pdfs.ingest import collect_selected_pdfs
    if args.workers < 1 or args.venue_workers < 1 or (args.max_searches is not None and args.max_searches < 0):
        parser.error("Invalid concurrency/search limit")
    return collect_selected_pdfs(args)


def query_bibliography(args, parser):
    records = DblpCollector(args.stream, args.year, args.doi).collect(args.limit, args.page_size)
    write_jsonl(args.output, records)
    return {"bibliographic_records": len(records), "output": str(args.output)}


def download_pdf(args, parser):
    if not re.fullmatch(r"[0-9a-f]{64}", args.paper_id):
        parser.error("paper_id 应为清单中的 64 位十六进制字符串。")

    with args.manifest.open(encoding="utf-8") as source:
        records = {record["paper_id"]: record for record in map(json.loads, source)}

    paper = records.get(args.paper_id)
    if paper is None:
        raise SystemExit("清单里找不到该 paper_id。")
    pdf_url = args.pdf_url or next(iter(paper.get("pdf_urls") or []), None)
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

    index = PaperPageIndex(args.index, args.es_url)
    indexed_count = index.index_pages(args.paper_id, pages)
    print(f"索引 {args.index} 已核对 {indexed_count} 页")
    if args.phrase is not None:
        index.verify_phrase(args.paper_id, len(pages), args.phrase, args.expected_page)


def main():
    from dotenv import load_dotenv
    root = argparse.ArgumentParser(prog="python -m scripts.data_collect", description="论文采集、检索、PDF 入库和精筛")
    commands = root.add_subparsers(dest="command", required=True)

    def command(name, handler, help):
        parser = commands.add_parser(name, help=help)
        parser.set_defaults(handler=handler, command_parser=parser)
        parser.add_argument("--env-file", type=Path, help="额外的本机环境配置；已有环境变量优先")
        return parser

    def retrieval_options(parser):
        parser.add_argument("--profile", required=True, type=Path, help="此命令使用的 JSON 配置文件")
        parser.add_argument("--cache", type=Path, default=Path("data/embeddings/cache.sqlite3"))
        parser.add_argument("--es-url", default="http://127.0.0.1:9200")

    parser = command("collect", collect, "按配置从官方来源采集元数据")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--sources", nargs="+")
    parser.add_argument("--years", required=True, nargs="+", type=int)
    parser.add_argument("--as-of", default=date.today().isoformat())
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--previous", type=Path, help="上一快照；更新所选分区并保留其他范围，采样只补充记录")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--limit-per-source", type=int, help="每来源年份的采样数量；省略为全部")

    parser = command("build-corpus", build_corpus, "上传快照并由 Spark 校验、建表和统计")
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("--hdfs-root", default="/papers/corpora")

    parser = command("index-corpus", index_corpus, "复用或生成向量并建立 ES 论文索引")
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("--no-activate", action="store_true", help="建立索引并保留当前默认检索范围")
    parser.add_argument("--hdfs-root", default="/papers/corpora")
    retrieval_options(parser)

    parser = command("screen", screen, "ES 全库打分、RRF 粗筛并保存运行结果")
    parser.add_argument("--index", default="papers-current")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--per-topic", type=int)
    parser.add_argument("--venues", nargs="+")
    parser.add_argument("--years", nargs="+", type=int)
    parser.add_argument("--hdfs-root", default="/papers/screening")
    retrieval_options(parser)

    parser = command("search", search, "在全库或某次入选范围内重新检索")
    parser.add_argument("--index", default="papers-current")
    parser.add_argument("--text", required=True)
    parser.add_argument("--run-id", help="仅检索该运行的入选论文，不改历史分数")
    parser.add_argument("--venues", nargs="+")
    parser.add_argument("--years", nargs="+", type=int)
    parser.add_argument("--top", type=int, default=20)
    parser.add_argument("--output", type=Path)
    retrieval_options(parser)

    parser = command("collect-pdfs", collect_pdfs, "只为粗筛结果下载 PDF 并入库，可续跑")
    parser.add_argument("--selection", required=True, type=Path, help="含 summary.json 和 selected.jsonl 的筛选结果目录")
    parser.add_argument("--config", type=Path, default=HERE / "config" / "venues.json", help="出版平台与 PDF 回退入口")
    parser.add_argument("--output", type=Path, default=Path("data/pdf_ingest"))
    parser.add_argument("--hdfs-root", default="/papers/selected")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--venue-workers", type=int, default=1)
    parser.add_argument("--max-searches", type=int)

    parser = command("query-bibliography", query_bibliography, "查询 DBLP 书目信息，不生成候选快照")
    parser.add_argument("--stream", required=True)
    parser.add_argument("--year", required=True, type=int)
    parser.add_argument("--doi")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument("--output", required=True, type=Path)

    parser = command("download-pdf", download_pdf, "检查指定论文的公开 PDF 下载")
    parser.add_argument("manifest", type=Path)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("paper_id")
    parser.add_argument("--pdf-url")

    parser = command("extract-pages", extract_pages, "提取已入库 PDF 的逐页文本")
    parser.add_argument("pdf", type=Path)
    parser.add_argument("output", type=Path)

    parser = command("index-pages", index_pages, "索引页文本并验证页码和高亮")
    parser.add_argument("input_uri")
    parser.add_argument("paper_id")
    parser.add_argument("--index", default="paper-pages")
    parser.add_argument("--es-url", default="http://127.0.0.1:9200")
    parser.add_argument("--expected-pages", type=int)
    parser.add_argument("--phrase")
    parser.add_argument("--expected-page", type=int)

    fine_screening_cli.register_commands(commands.add_parser("fine-screen", help="逐篇评估论文复现条件"))

    args = root.parse_args()
    if args.command == "fine-screen":
        return fine_screening_cli.execute(args)
    load_dotenv(HERE.parents[1] / ".env")
    if args.env_file:
        load_dotenv(args.env_file, override=False)
    result = args.handler(args, args.command_parser)
    if result is not None:
        if args.command == "screen":
            result = {key: result[key] for key in ("run_id", "index", "papers_scored", "selected_count", "scoring_seconds", "hdfs_root")}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if args.command == "collect" and result["collection_status"] != "complete":
            raise SystemExit(1)


if __name__ == "__main__":
    raise SystemExit(main())

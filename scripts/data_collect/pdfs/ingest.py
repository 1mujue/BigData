"""Resumable PDF ingestion restricted to a completed screening selection."""
import json
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from threading import Lock

from ..network import FetchError, HttpClient
from .download import OpenAlexPdfResolver, PdfResolver
from ..retrieval.screening import load_selection
from .candidates import FirecrawlSearch, SourceCatalog
from ..storage import HDFS_URI, HdfsStore
from ..tool import read_jsonl, spark_session, write_json, write_jsonl


MANIFEST_SCHEMA = ("paper_id STRING, venue STRING, year INT, status STRING, source STRING, "
                   "source_url STRING, version STRING, hdfs_path STRING, sha256 STRING, bytes LONG, pages INT, "
                   "reason STRING, first_screening_run STRING")


def store_pdfs(results, store, root):
    paths = [Path(result["local_path"]) for result in results]
    store.put_many(paths, root + "/pdfs")
    for result, path in zip(results, paths):
        result.update(status="stored", hdfs_path=root + "/pdfs/" + path.name)
        path.unlink()
        result.pop("local_path", None)


def resume_states(journal, remote_sizes):
    states = {row["paper_id"]: row for row in read_jsonl(journal)} if journal.exists() else {}
    for pid, state in list(states.items()):
        if state["status"] == "downloaded":
            continue
        if state["status"] != "stored" or remote_sizes.get(state.get("hdfs_path")) != state.get("bytes"):
            states.pop(pid)
    return states


def collect_selected_pdfs(args):
    selection, papers = load_selection(args.selection)
    if not papers:
        raise ValueError("Screening selection is empty")
    config = json.loads(args.config.read_text(encoding="utf-8"))
    if not args.hdfs_root.startswith("/papers/") or ".." in args.hdfs_root.split("/"):
        raise ValueError("PDF storage root must be below /papers/")
    run_dir = args.output.resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    contract = {"kind": "selected-pdf-store", "hdfs_root": args.hdfs_root}
    contract_path = run_dir / "store.json"
    if contract_path.exists():
        if json.loads(contract_path.read_text()) != contract:
            raise ValueError("Output directory belongs to a different PDF store")
    else:
        if (run_dir / "collection.json").exists() or (run_dir / "downloads.jsonl").exists():
            raise ValueError("Use a fresh selected-PDF output directory")
        write_json(contract_path, contract)
    client = HttpClient(run_dir / "cache")
    store = HdfsStore()
    store.prepare(args.hdfs_root)
    issues = []
    journal = run_dir / "downloads.jsonl"
    remote_sizes = store.sizes(args.hdfs_root + "/pdfs")
    admitted = {r["paper_id"]: r for r in read_jsonl(journal)} if journal.exists() else {}
    states = resume_states(journal, remote_sizes)
    catalog = SourceCatalog(client, config)
    resolver = PdfResolver(client, run_dir / "staging")
    search = FirecrawlSearch(client)
    groups = defaultdict(list)
    for paper in papers:
        groups[(paper["venue"], paper["year"])].append(paper)
    searches = 0
    search_errors = {}
    journal_lock = Lock()
    search_lock = Lock()

    def record(paper, result):
        origin = admitted.get(paper["paper_id"], {}).get("first_screening_run", selection["run_id"])
        result.update(venue=paper["venue"], year=paper["year"], first_screening_run=origin)
        admitted[paper["paper_id"]] = result
        with journal_lock:
            states[paper["paper_id"]] = result
            with journal.open("a", encoding="utf-8") as output:
                output.write(json.dumps(result, ensure_ascii=False) + "\n")

    def pending(group):
        return [paper for paper in group if states.get(paper["paper_id"], {}).get("status") != "stored"]

    def download_stage(group, candidates, stage):
        jobs = [(paper, candidates.get(paper["paper_id"], [])) for paper in pending(group)]
        for start in range(0, len(jobs), 25):
            batch = jobs[start:start + 25]
            ready = []
            with ThreadPoolExecutor(max_workers=args.workers) as executor:
                results = executor.map(lambda job: resolver.download(*job), batch)
                for (paper, _), result in zip(batch, results):
                    previous = states.get(paper["paper_id"], {})
                    result["attempts"] = previous.get("attempts", []) + result["attempts"]
                    record(paper, result)
                    if result["status"] == "downloaded":
                        ready.append((paper, result))
            # A storage failure propagates; downloaded files and the journal survive.
            store_pdfs([result for _, result in ready], store, args.hdfs_root)
            for paper, result in ready:
                record(paper, result)
            print(f"{stage}: {len(ready)}/{len(batch)} stored", flush=True)

    # Finish interrupted uploads before asking any source to download again.
    uploads = []
    for paper in papers:
        state = states.get(paper["paper_id"], {})
        if state.get("status") == "downloaded" and Path(state.get("local_path", "")).is_file():
            uploads.append((paper, state))
    for start in range(0, len(uploads), 25):
        batch = uploads[start:start + 25]
        store_pdfs([state for _, state in batch], store, args.hdfs_root)
        for paper, state in batch:
            record(paper, state)

    def process_group(venue, year, group):
        nonlocal searches
        if not pending(group):
            print(f"{venue} {year}: already stored, skipped", flush=True)
            return
        try:
            catalog.official_index(venue, year)
        except (FetchError, ValueError) as error:
            issues.append({"stage": "official_index", "venue": venue, "year": year, "error": str(error)})
        download_stage(group, {p["paper_id"]: catalog.direct(p) for p in pending(group)}, f"{venue} {year} official")
        remaining = pending(group)
        if remaining:
            try:
                resolved = OpenAlexPdfResolver(client).resolve(remaining)
                download_stage(group, resolved, f"{venue} {year} OpenAlex")
            except (FetchError, ValueError) as error:
                issues.append({"stage": "openalex", "venue": venue, "year": year, "error": str(error)})
        if pending(group) and config.get(venue, {}).get("researchr"):
            try:
                catalog.prepare_researchr(venue, year)
                candidates = {}
                for paper in pending(group):
                    try:
                        candidates[paper["paper_id"]] = catalog.researchr(paper)
                    except (FetchError, ValueError) as error:
                        issues.append({"stage": "researchr", "paper_id": paper["paper_id"], "error": str(error)})
                download_stage(group, candidates, f"{venue} {year} Researchr")
            except (FetchError, ValueError) as error:
                issues.append({"stage": "researchr_index", "venue": venue, "year": year, "error": str(error)})
        candidates = {}
        for paper in pending(group):
            with search_lock:
                cached = search.cached(paper)
                if not cached and (not search.key or (args.max_searches is not None and searches >= args.max_searches)):
                    continue
                try:
                    if not cached:
                        searches += 1
                    candidates[paper["paper_id"]] = search.search(paper)
                except (FetchError, ValueError) as error:
                    search_errors[paper["paper_id"]] = str(error)
                    issues.append({"stage": "firecrawl", "paper_id": paper["paper_id"], "error": str(error)})
                    print(f"{venue} {year} Firecrawl {paper['paper_id']}: {error}", flush=True)
        if candidates:
            download_stage(group, candidates, f"{venue} {year} Firecrawl")

    with ThreadPoolExecutor(max_workers=args.venue_workers) as executor:
        futures = [executor.submit(process_group, venue, year, group)
                   for (venue, year), group in groups.items()]
        for future in as_completed(futures):
            future.result()

    result_dir = run_dir / "runs" / selection["run_id"]
    manifest = result_dir / "manifest.jsonl"
    for paper in pending(papers):
        state = states[paper["paper_id"]]
        if search.cached(paper):
            state["reason"] = "no_downloadable_candidate"
        elif not search.key:
            state["reason"] = "search_not_configured"
        elif paper["paper_id"] in search_errors:
            state["reason"] = "search_service_failed"
        else:
            state["reason"] = "search_limit_reached"
        record(paper, state)
    write_jsonl(manifest, [states[p["paper_id"]] for p in papers])
    remote_run = args.hdfs_root + "/runs/" + selection["run_id"]
    store.dfs("-mkdir", "-p", remote_run + "/tables")
    import getpass
    store.dfs("-chown", getpass.getuser(), remote_run + "/tables")
    store.put(manifest, remote_run + "/manifest.jsonl")
    base = HDFS_URI + remote_run
    with spark_session("collect-selected-pdfs") as spark:
        spark.read.schema(MANIFEST_SCHEMA).json(base + "/manifest.jsonl").write.mode("overwrite").parquet(base + "/tables/downloads")
    selected_states = [states[p["paper_id"]] for p in papers]
    summary = {"screening_run": selection["run_id"], "selection_sha256": selection["selection_sha256"],
               "papers": len(papers), "statuses": dict(Counter(r["status"] for r in selected_states)),
               "sources": dict(Counter(r.get("source") for r in selected_states if r["status"] == "stored")),
               "firecrawl_queries": searches, "firecrawl_enabled": bool(search.key), "issues": issues,
               "hdfs_root": args.hdfs_root, "hdfs_run": remote_run}
    summary["by_venue_year"] = {f"{venue}-{year}": dict(Counter(states[p["paper_id"]]["status"] for p in group)) for (venue, year), group in groups.items()}
    write_json(result_dir / "summary.json", summary)
    store.put(result_dir / "summary.json", remote_run + "/summary.json")
    return summary

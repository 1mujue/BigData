"""DBLP collection, resumable PDF acquisition and HDFS ingestion."""
import getpass
import hashlib
import json
import shlex
import subprocess
import tarfile
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory

from .metadata import DblpCollector, MetadataTable
from .network import FetchError, HttpClient
from .pdf import OpenAlexPdfResolver, PdfResolver
from .sources import FirecrawlSearch, SourceCatalog
from .tool import spark_session, write_jsonl


MANIFEST_SCHEMA = ("paper_id STRING, venue STRING, year INT, status STRING, source STRING, "
                   "source_url STRING, version STRING, hdfs_path STRING, sha256 STRING, bytes LONG, pages INT, reason STRING")


class HdfsStore:
    def __init__(self):
        self.command = ["docker", "compose", "-f", str(Path(__file__).parent / "docker/compose.yaml"), "exec", "-T", "namenode"]

    def dfs(self, *args, input_file=None):
        return subprocess.run([*self.command, "hdfs", "dfs", *args], stdin=input_file,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True).stdout.decode()

    def prepare(self, root):
        self.dfs("-mkdir", "-p", root + "/pdfs", root + "/tables")
        self.dfs("-chown", getpass.getuser(), root + "/tables")

    def put(self, path, destination):
        with Path(path).open("rb") as source:
            self.dfs("-put", "-f", "-", destination, input_file=source)
        if int(self.dfs("-stat", "%b", destination).strip()) != Path(path).stat().st_size:
            raise OSError("HDFS file length differs after upload")

    def put_many(self, paths, directory):
        if not paths:
            return
        with TemporaryDirectory() as temp:
            archive = Path(temp) / "pdfs.tar"
            with tarfile.open(archive, "w") as output:
                for path in paths:
                    output.add(path, arcname=path.name)
            script = ('set -eu; stage=$(mktemp -d /tmp/paper-ingest-XXXXXXXX); '
                      'trap \'rm -rf -- "$stage"\' EXIT; tar -xf - -C "$stage"; '
                      'hdfs dfs -put -f "$stage"/*.pdf ' + shlex.quote(directory))
            with archive.open("rb") as source:
                subprocess.run([*self.command, "sh", "-c", script], stdin=source, capture_output=True, check=True)
        remote = self.dfs("-stat", "%n %b", *(directory + "/" + path.name for path in paths))
        sizes = {name: int(size) for name, size in (line.split() for line in remote.splitlines())}
        if any(sizes.get(path.name) != path.stat().st_size for path in paths):
            raise OSError("HDFS batch length verification failed")

    def sizes(self, directory):
        listing = self.dfs("-ls", directory)
        return {line.split()[-1]: int(line.split()[4]) for line in listing.splitlines() if line.startswith("-")}


def store_pdfs(results, store, root):
    paths = [Path(result["local_path"]) for result in results]
    store.put_many(paths, root + "/pdfs")
    for result, path in zip(results, paths):
        result.update(status="stored", hdfs_path=root + "/pdfs/" + path.name)
        path.unlink()
        result.pop("local_path", None)


def read_jsonl(path):
    with path.open(encoding="utf-8") as source:
        return [json.loads(line) for line in source if line.strip()]


def resume_states(journal, remote_sizes):
    states = {row["paper_id"]: row for row in read_jsonl(journal)} if journal.exists() else {}
    for pid, state in list(states.items()):
        if state["status"] == "downloaded":
            continue
        if state["status"] != "stored" or remote_sizes.get(state.get("hdfs_path")) != state.get("bytes"):
            states.pop(pid)
    return states


def collect_and_download(args):
    config = json.loads(args.config.read_text(encoding="utf-8"))
    names = args.venues or list(config)
    unknown = set(names) - config.keys()
    if unknown:
        raise ValueError(f"Unknown venues: {sorted(unknown)}")
    if not args.hdfs_root.startswith("/papers/") or ".." in args.hdfs_root.split("/"):
        raise ValueError("--hdfs-root must be a collection directory below /papers/")
    run_dir = args.output.resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    contract = {"venues": names, "years": args.years, "limit": args.limit_per_venue,
                "hdfs_root": args.hdfs_root, "config": {name: config[name] for name in names}}
    contract_path = run_dir / "collection.json"
    if contract_path.exists() and json.loads(contract_path.read_text()) != contract:
        raise ValueError("This output directory belongs to a different collection; choose another output directory")
    contract_path.write_text(json.dumps(contract, ensure_ascii=False, indent=2), encoding="utf-8")
    client = HttpClient(run_dir / "cache")
    store = HdfsStore()
    store.prepare(args.hdfs_root)
    issues = []
    papers = []
    for name in names:
        for year in args.years:
            path = run_dir / "raw" / f"{name}-{year}.jsonl"
            try:
                if path.exists():
                    rows = read_jsonl(path)
                else:
                    settings = {**config[name], **config[name].get("overrides", {}).get(str(year), {})}
                    rows = DblpCollector(settings["stream"], year, issue=settings.get("issue"), client=client).collect(args.limit_per_venue, 100)
                    for row in rows:
                        row.update(venue=name, paper_id=hashlib.sha256(row["dblp_id"].encode()).hexdigest())
                    write_jsonl(path, rows)
                papers.extend(rows)
                print(f"DBLP {name} {year}: {len(rows)} papers", flush=True)
            except (FetchError, ValueError) as error:
                issues.append({"stage": "dblp", "venue": name, "year": year, "error": str(error)})
                print(f"DBLP {name} {year}: {error}", flush=True)
    papers = list({paper["paper_id"]: paper for paper in papers}.values())
    if not papers:
        raise ValueError("No DBLP papers collected; inspect source errors")
    metadata_path = run_dir / "metadata.jsonl"
    write_jsonl(metadata_path, papers)
    store.put(metadata_path, args.hdfs_root + "/metadata.jsonl")

    journal = run_dir / "downloads.jsonl"
    remote_sizes = store.sizes(args.hdfs_root + "/pdfs")
    states = resume_states(journal, remote_sizes)
    catalog = SourceCatalog(client, config)
    resolver = PdfResolver(client, run_dir / "staging")
    search = FirecrawlSearch(client)
    groups = defaultdict(list)
    for paper in papers:
        groups[(paper["venue"], paper["year"])].append(paper)
    searches = 0
    search_error = None

    def record(paper, result):
        result.update(venue=paper["venue"], year=paper["year"])
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
    for paper in papers:
        state = states.get(paper["paper_id"], {})
        if state.get("status") == "downloaded" and Path(state.get("local_path", "")).is_file():
            store_pdfs([state], store, args.hdfs_root)
            record(paper, state)

    for (venue, year), group in groups.items():
        if not pending(group):
            print(f"{venue} {year}: already stored, skipped", flush=True)
            continue
        try:
            catalog.official_index(venue, year)
        except (FetchError, ValueError) as error:
            issues.append({"stage": "official_index", "venue": venue, "year": year, "error": str(error)})
        download_stage(group, {p["paper_id"]: catalog.direct(p) for p in pending(group)}, f"{venue} {year} official")
        remaining = pending(group)
        if remaining:
            try:
                resolved = OpenAlexPdfResolver(client).resolve(remaining)
                download_stage(group, {r["paper_id"]: r["candidates"] for r in resolved}, f"{venue} {year} OpenAlex")
            except (FetchError, ValueError) as error:
                issues.append({"stage": "openalex", "venue": venue, "year": year, "error": str(error)})
        if pending(group) and config[venue].get("researchr"):
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
            cached = search.cached(paper)
            if not cached and (not search.key or search_error or (args.max_searches is not None and searches >= args.max_searches)):
                continue
            try:
                if not cached:
                    searches += 1
                candidates[paper["paper_id"]] = search.search(paper)
            except (FetchError, ValueError) as error:
                search_error = str(error)
                issues.append({"stage": "firecrawl", "error": search_error})
        if candidates:
            download_stage(group, candidates, f"{venue} {year} Firecrawl")

    manifest = run_dir / "manifest.jsonl"
    for paper in pending(papers):
        state = states[paper["paper_id"]]
        if search.cached(paper):
            state["reason"] = "no_downloadable_candidate"
        elif not search.key:
            state["reason"] = "search_not_configured"
        elif search_error:
            state["reason"] = "search_service_failed"
        else:
            state["reason"] = "search_limit_reached"
        record(paper, state)
    write_jsonl(manifest, [states[p["paper_id"]] for p in papers])
    store.put(manifest, args.hdfs_root + "/manifest.jsonl")
    base = "hdfs://127.0.0.1:8020" + args.hdfs_root
    with spark_session("collect-pdfs") as spark:
        MetadataTable(spark).build(base + "/metadata.jsonl", base + "/tables/papers", mode="overwrite")
        spark.read.schema(MANIFEST_SCHEMA).json(base + "/manifest.jsonl").write.mode("overwrite").parquet(base + "/tables/downloads")
    summary = {"papers": len(papers), "statuses": dict(Counter(r["status"] for r in states.values())),
               "sources": dict(Counter(r.get("source") for r in states.values() if r["status"] == "stored")),
               "firecrawl_queries": searches, "firecrawl_enabled": bool(search.key), "issues": issues,
               "hdfs_root": args.hdfs_root}
    summary["by_venue_year"] = {f"{venue}-{year}": dict(Counter(states[p["paper_id"]]["status"] for p in group)) for (venue, year), group in groups.items()}
    (run_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return summary

"""Collect configured official catalogs and update their corpus partitions."""
import json
from collections import Counter
from datetime import date

from ..corpus import create_snapshot, load_snapshot, normalize_records
from ..tool import write_json
from .adapters import Collector
from .http import Fetcher, FetchError
from .model import finalize


def merge_partitions(previous, updates, replaced):
    """Replace complete partitions; retain other records, including during sampling."""
    updates = [finalize(row) for row in normalize_records(updates, previous)]
    updated_ids = {row["paper_id"] for row in updates}
    carried = [row for row in previous
               if (row["venue"], row["year"]) not in replaced and row["paper_id"] not in updated_ids]
    return carried + updates, Counter((row["venue"], row["year"]) for row in carried)


def collect_sources(args):
    config = json.loads(args.config.read_text(encoding="utf-8"))
    names = args.sources or list(config)
    if not names or set(names) - config.keys():
        raise ValueError("Requested sources must exist in the source configuration")
    if args.workers < 1 or (args.limit_per_source is not None and args.limit_per_source < 1):
        raise ValueError("Workers and sample limit must be positive")
    date.fromisoformat(args.as_of)
    if any(not 1000 <= year <= 9999 for year in args.years):
        raise ValueError("Collection years must be four-digit integers")
    if (args.output / "manifest.json").exists():
        raise ValueError("Snapshot already exists; choose a new output directory")
    prior_manifest, previous = load_snapshot(args.previous) if args.previous else ({}, [])
    prior_counts = Counter((row["venue"], row["year"]) for row in previous)
    fetch = Fetcher(args.output / "cache", refresh=args.refresh)
    updates, partitions, replaced = [], {}, set()
    failed = False
    for name in dict.fromkeys(names):
        settings = config[name]
        for year in sorted(set(args.years)):
            key = (name, year)
            partition = {"venue": name, "year": year}
            partitions[key] = partition
            if year in settings.get("not_held_years", []):
                partition.update(status="not_held", collected_count=0)
                replaced.add(key)
                continue
            collector = Collector(fetch, settings, workers=args.workers, as_of=args.as_of,
                                  limit=args.limit_per_source)
            try:
                rows = collector.collect(name, year)
                status = "sampled" if args.limit_per_source else "complete"
                if collector.errors:
                    failed = True
                    status = "retained_after_failure" if prior_counts[key] else "partial"
                else:
                    if not args.limit_per_source:
                        replaced.add(key)
                if status != "retained_after_failure":
                    updates.extend(finalize(row) for row in rows)
                partition.update(status=status, collected_count=len(rows), stats=collector.stats,
                                 warnings=collector.warnings, errors=collector.errors,
                                 detail_failures=sum(bool(row["errors"]) for row in rows))
                print(f"{name} {year}: {len(rows)} collected ({status})", flush=True)
            except (FetchError, ValueError) as error:
                failed = True
                partition.update(status="retained_after_failure" if prior_counts[key] else "failed",
                                 collected_count=0, error=str(error))
                print(f"{name} {year}: {error}", flush=True)
    records, carried = merge_partitions(previous, updates, replaced)
    counts = Counter((row["venue"], row["year"]) for row in records)
    for key in sorted(set(partitions) | counts.keys()):
        partition = partitions.setdefault(key, {"venue": key[0], "year": key[1],
                                                 "status": "carried", "collected_count": 0})
        partition.update(count=counts[key], carried_count=carried[key])
        if carried[key]:
            partition["previous_snapshot"] = prior_manifest["snapshot_id"]
    manifest = create_snapshot(records, args.output, partitions=list(partitions.values()),
        provenance={"collector": "official-catalogs", "as_of": args.as_of,
                    "sources": names, "years": args.years, "sample_limit": args.limit_per_source,
                    "config": {name: config[name] for name in names}, "requests": fetch.counts,
                    "previous_snapshot": prior_manifest.get("snapshot_id")})
    manifest["collection_status"] = "partial" if failed else "complete"
    write_json(args.output / "manifest.json", manifest)
    return {"snapshot_id": manifest["snapshot_id"], "count": manifest["count"],
            "collection_status": manifest["collection_status"], "coverage": manifest["coverage"],
            "partitions": list(partitions.values()), "output": str(args.output)}

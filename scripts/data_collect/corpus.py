"""Canonical metadata snapshots, independent of retrieval and PDF availability."""
import hashlib
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from .storage import HDFS_URI
from .tool import file_sha256, read_jsonl, write_json, write_jsonl


def now():
    return datetime.now(timezone.utc).isoformat()


def aliases(row):
    venue = row.get("venue")
    result = [(venue, "source_key", row.get("source_key") or row.get("id"))]
    for field in ("doi", "openreview_id", "paper_url"):
        value = row.get(field)
        if value:
            result.append((venue, field, value.lower() if field == "doi" else value))
    return result


def normalize_records(records, previous=()):
    prior = {r["paper_id"]: r for r in previous}
    by_alias = defaultdict(set)
    for row in previous:
        for key in aliases(row):
            by_alias[key].add(row["paper_id"])
    output = []
    seen = set()
    for original in records:
        row = dict(original)
        source_key = row.pop("id", None) or row.get("source_key")
        if not isinstance(source_key, str) or not source_key:
            raise ValueError("A paper requires a stable source_key")
        row["source_key"] = source_key
        candidates = set().union(*(by_alias[k] for k in aliases(row) if len(by_alias[k]) == 1))
        if len(candidates) > 1:
            raise ValueError(f"Conflicting previous identities for {source_key}")
        previous_row = prior[next(iter(candidates))] if candidates else None
        if previous_row:
            row["source_key"] = previous_row["source_key"]
            row["paper_id"] = previous_row["paper_id"]
            # Preserve obtained fields when the publisher currently omits them.
            for field in ("abstract", "doi", "openreview_id", "authors", "tags", "pdf_urls"):
                if not row.get(field) and previous_row.get(field):
                    row[field] = previous_row[field]
                    sources = previous_row.get("field_sources", {}).get(field)
                    if sources:
                        row.setdefault("field_sources", {})[field] = sources
        else:
            row["paper_id"] = row.get("paper_id") or hashlib.sha256(source_key.encode()).hexdigest()
        if not re.fullmatch(r"[0-9a-f]{64}", row["paper_id"]):
            raise ValueError(f"Invalid paper_id for {source_key}")
        if not isinstance(row.get("title"), str) or not row["title"].strip():
            raise ValueError(f"Missing title: {source_key}")
        if not isinstance(row.get("year"), int) or not 1000 <= row["year"] <= 9999 or not row.get("venue"):
            raise ValueError(f"Invalid venue/year: {source_key}")
        if row["paper_id"] in seen:
            raise ValueError(f"Duplicate paper identity: {source_key}")
        seen.add(row["paper_id"])
        output.append(row)
    return output


def create_snapshot(records, output, *, provenance=None, previous=(), partitions=None):
    output = Path(output)
    if (output / "manifest.json").exists():
        raise ValueError("Snapshot already exists; choose a new output directory")
    rows = normalize_records(records, previous)
    if not rows:
        raise ValueError("Cannot publish an empty corpus")
    write_jsonl(output / "papers.jsonl", rows)
    digest = file_sha256(output / "papers.jsonl")
    manifest = {
        "schema_version": 1, "kind": "paper-corpus", "snapshot_id": digest,
        "created_at": now(), "count": len(rows), "provenance": provenance or {},
        "by_venue_year": dict(sorted(Counter(f'{r["venue"]}-{r["year"]}' for r in rows).items())),
        "coverage": {field: sum(bool(r.get(field)) for r in rows)
                     for field in ("title", "abstract", "doi", "tags", "pdf_urls")},
        "partitions": partitions or [],
    }
    write_json(output / "manifest.json", manifest)
    return manifest


def load_snapshot(directory):
    directory = Path(directory)
    manifest = json.loads((directory / "manifest.json").read_text())
    if manifest.get("kind") != "paper-corpus" or file_sha256(directory / "papers.jsonl") != manifest["snapshot_id"]:
        raise ValueError("Corpus manifest does not match papers.jsonl")
    rows = read_jsonl(directory / "papers.jsonl")
    if len(rows) != manifest["count"] or len({r["paper_id"] for r in rows}) != len(rows):
        raise ValueError("Corpus count or identities differ from the manifest")
    return manifest, rows


def hdfs_corpus_root(base, manifest):
    if not base.startswith("/papers/") or ".." in base.split("/"):
        raise ValueError("HDFS corpus base must be below /papers/")
    return base.rstrip("/") + "/" + manifest["snapshot_id"]


def build_corpus(directory, hdfs_base="/papers/corpora"):
    from .storage import HdfsStore
    from .tool import spark_session

    directory = Path(directory)
    manifest, _ = load_snapshot(directory)
    root = hdfs_corpus_root(hdfs_base, manifest)
    store = HdfsStore()
    store.dfs("-mkdir", "-p", root + "/raw", root + "/tables")
    import getpass
    store.dfs("-chown", getpass.getuser(), root + "/tables")
    for name in ("papers.jsonl", "manifest.json"):
        store.put(directory / name, root + "/raw/" + name)
    remote_hash = hashlib.sha256(store.dfs("-cat", root + "/raw/papers.jsonl").encode()).hexdigest()
    if remote_hash != manifest["snapshot_id"]:
        raise OSError("HDFS corpus content differs from the local snapshot")
    uri = HDFS_URI + root
    with spark_session("build-paper-corpus") as spark:
        table = MetadataTable(spark)
        table.validate(uri + "/raw/papers.jsonl", manifest["count"])
        stats = table.build(uri + "/raw/papers.jsonl", uri + "/tables/papers", mode="overwrite")
    write_json(directory / "storage.json", {"hdfs_root": root, "tables": stats})
    return {"hdfs_root": root, "count": manifest["count"], "tables": stats}


class MetadataTable:
    SCHEMA = ("paper_id STRING, source_key STRING, title STRING, year INT, venue STRING, "
              "abstract STRING, doi STRING, dblp_id STRING, openreview_id STRING, "
              "authors ARRAY<STRING>, tags ARRAY<STRUCT<kind: STRING, value: STRING, source: STRING>>, "
              "paper_url STRING, pdf_urls ARRAY<STRING>, source_urls ARRAY<STRING>, "
              "publication_date STRING, collected_at STRING, "
              "scope STRUCT<status: STRING, track: STRING, type: STRING, source_venue: STRING>, "
              "field_sources MAP<STRING, ARRAY<STRING>>")

    def __init__(self, spark):
        self.spark = spark

    def validate(self, input_uri, expected_count):
        stats = self._read_jsonl(input_uri).selectExpr(
            "count(*) AS total_rows", "count(DISTINCT paper_id) AS unique_ids",
            """count_if(coalesce(trim(source_key), '') = ''
                OR coalesce(trim(title), '') = '' OR year IS NULL
                OR coalesce(trim(venue), '') = ''
                OR NOT coalesce(paper_id RLIKE '^[0-9a-f]{64}$', false)) AS invalid_rows""",
        ).first().asDict()
        if stats["total_rows"] != expected_count or stats["unique_ids"] != expected_count or stats["invalid_rows"]:
            raise ValueError(f"Corpus validation failed: {stats}; expected {expected_count}")
        return stats

    def build(self, input_uri, output_uri, *, mode="errorifexists"):
        papers = self._read_jsonl(input_uri).dropDuplicates(["paper_id"])
        papers.write.mode(mode).parquet(output_uri)
        saved = self.spark.read.parquet(output_uri)
        saved.createOrReplaceTempView("papers")
        stats = [r.asDict() for r in self.spark.sql("""
            SELECT venue, year, count(*) AS papers,
              count_if(coalesce(abstract, '') <> '') AS with_abstract,
              count_if(coalesce(doi, '') <> '') AS with_doi,
              count_if(size(pdf_urls) > 0) AS with_pdf_link
            FROM papers GROUP BY venue, year ORDER BY venue, year
        """).collect()]
        return stats

    def _read_jsonl(self, uri):
        from pyspark.sql import functions as F
        return self.spark.read.text(uri).select(
            F.from_json("value", self.SCHEMA, {"mode": "FAILFAST"}).alias("paper"),
            F.get_json_object("value", "$.publication").alias("publication_json"),
        ).select("paper.*", "publication_json")

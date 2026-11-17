"""Elasticsearch BM25/exact-vector retrieval and reproducible RRF screening."""
import hashlib
import json
import time
from collections import defaultdict
from pathlib import Path

import numpy as np

from ..corpus import hdfs_corpus_root, load_snapshot, now
from .elastic import Elasticsearch
from .embeddings import EmbeddingAPI, EmbeddingCache, document_text, query_text, vector_signature
from ..storage import HDFS_URI
from ..tool import file_sha256, read_jsonl, write_json, write_jsonl


METHOD = "es-bm25-exact-cosine-rrf-v1"
RUN_INDEX = "paper-screening-runs"
RESULT_INDEX = "paper-screening-results"


def load_profile(path, command):
    profile = json.loads(Path(path).read_text())
    embedding = profile["embedding"]
    if not 1 <= embedding["dimensions"] <= 4096 or not embedding.get("model") or not embedding.get("endpoint"):
        raise ValueError("Invalid embedding configuration")
    if command == "index-corpus":
        return profile
    if profile.get("rrf_k", 60) < 1:
        raise ValueError("RRF k must be positive")
    fields = profile.get("fields")
    if "fields" in profile and (not isinstance(fields, list) or not fields or
                                not all(isinstance(f, str) and f.strip() for f in fields)):
        raise ValueError("fields must be a nonempty list of search fields")
    if command == "search":
        return profile
    topics = profile.get("topics")
    if not topics:
        raise ValueError("Screening requires at least one topic in the profile")
    if len({t["id"] for t in topics}) != len(topics):
        raise ValueError("Topics must have unique IDs")
    if any(not t.get("queries") or any(not q.strip() for q in t["queries"]) for t in topics):
        raise ValueError("Every topic requires nonempty query text")
    return profile


def paper_mapping(dimensions, meta):
    return {"settings": {"number_of_shards": 1, "number_of_replicas": 0}, "mappings": {
        "dynamic": False, "_meta": meta, "properties": {
            "paper_id": {"type": "keyword"}, "source_key": {"type": "keyword"},
            "snapshot_id": {"type": "keyword"}, "title": {"type": "text"},
            "abstract": {"type": "text"}, "venue": {"type": "keyword"}, "year": {"type": "integer"},
            "authors": {"type": "text", "fields": {"raw": {"type": "keyword"}}},
            "doi": {"type": "keyword"}, "openreview_id": {"type": "keyword"},
            "tags": {"properties": {"kind": {"type": "keyword"}, "value": {"type": "text"},
                                     "source": {"type": "keyword", "index": False}}},
            "scope": {"properties": {"status": {"type": "keyword"}, "track": {"type": "keyword"},
                                      "type": {"type": "keyword"}}},
            "embedding": {"type": "dense_vector", "dims": dimensions, "index": False},
        }}}


def index_corpus(snapshot, profile, cache_path, *, es_url="http://127.0.0.1:9200", hdfs_base="/papers/corpora", activate=True):
    from ..storage import HdfsStore

    directory = Path(snapshot)
    manifest, rows = load_snapshot(directory)
    config = profile["embedding"]
    signature = vector_signature(config)
    index = f'papers-{manifest["snapshot_id"][:16]}-{signature[:12]}'
    es = Elasticsearch(es_url)
    if es.exists(index):
        meta = es.request("GET", f"/{index}/_mapping")[index]["mappings"]["_meta"]
        if meta["snapshot_id"] != manifest["snapshot_id"] or meta["vector_signature"] != signature:
            raise ValueError("Index belongs to a different corpus/model")
        count = es.request("POST", f"/{index}/_count", {"query": {"exists": {"field": "embedding"}}})["count"]
        if meta.get("ready"):
            if count != len(rows):
                raise ValueError("Completed corpus index has changed; inspect it before rebuilding")
            if activate:
                es.point_alias("papers-current", index)
            return {"index": index, "count": count, "new_vectors": 0}
        # Start an incomplete build afresh: overwritten documents otherwise remain
        # in Lucene's term statistics until segment merging changes BM25 scores.
        es.request("DELETE", "/" + index)
    cache = EmbeddingCache(cache_path, config)
    try:
        vectors = cache.get_or_create([document_text(row) for row in rows], EmbeddingAPI(config).encode)
        new_vectors, tokens = cache.new_vectors, cache.new_tokens
    finally:
        cache.close()
    vector_dir = directory / "vectors" / signature
    vector_dir.mkdir(parents=True, exist_ok=True)
    np.savez(vector_dir / "vectors.npz", paper_ids=np.asarray([r["paper_id"] for r in rows]), vectors=vectors)
    write_json(vector_dir / "manifest.json", {"snapshot_id": manifest["snapshot_id"], "embedding": config,
        "vector_signature": signature, "count": len(rows), "sha256": file_sha256(vector_dir / "vectors.npz")})
    store = HdfsStore()
    root = hdfs_corpus_root(hdfs_base, manifest) + "/vectors/" + signature
    store.dfs("-mkdir", "-p", root)
    for name in ("vectors.npz", "manifest.json"):
        store.put(vector_dir / name, root + "/" + name)
    meta = {"kind": "paper-corpus", "snapshot_id": manifest["snapshot_id"], "count": len(rows),
            "embedding": config, "vector_signature": signature, "ready": False}
    if not es.exists(index):
        es.request("PUT", "/" + index, paper_mapping(config["dimensions"], meta))
    es.bulk(index, ((row["paper_id"], {**row, "snapshot_id": manifest["snapshot_id"], "embedding": vector.tolist()})
                    for row, vector in zip(rows, vectors)))
    count = es.request("POST", f"/{index}/_count", {"query": {"exists": {"field": "embedding"}}})["count"]
    if count != len(rows):
        raise ValueError(f"Indexed vector count differs: {count}/{len(rows)}")
    meta["ready"] = True
    es.request("PUT", f"/{index}/_mapping", {"_meta": meta})
    if activate:
        es.point_alias("papers-current", index)
    return {"index": index, "count": count, "new_vectors": new_vectors, "api_tokens": tokens,
            "hdfs_vectors": root}


def filters_for(venues=None, years=None, ids=None):
    filters = []
    if venues:
        filters.append({"terms": {"venue": venues}})
    if years:
        filters.append({"terms": {"year": years}})
    if ids is not None:
        filters.append({"ids": {"values": ids}})
    return filters


def lexical_query(topic, fields):
    text = topic.get("lexical_query") or " ".join([*topic["queries"], *topic.get("keywords", [])])
    return {"multi_match": {"query": text, "fields": fields, "type": "best_fields"}}


def vector_query(vectors, filters):
    return {"dis_max": {"tie_breaker": 0, "queries": [
        {"script_score": {"query": {"bool": {"filter": [{"exists": {"field": "embedding"}}, *filters]}},
            "script": {"source": "cosineSimilarity(params.q, 'embedding') + 1.0",
                       "params": {"q": vector.tolist()}}}}
        for vector in vectors
    ]}}


def fuse_scores(lexical, semantic, k):
    """Full-population ranks; unreturned BM25 documents have zero lexical contribution."""
    keyword_order = sorted(lexical, key=lambda pid: (-lexical[pid], pid))
    vector_order = sorted(semantic, key=lambda pid: (-semantic[pid], pid))
    keyword_ranks = {pid: i + 1 for i, pid in enumerate(keyword_order)}
    vector_ranks = {pid: i + 1 for i, pid in enumerate(vector_order)}
    scores = {pid: 1 / (k + rank) + (1 / (k + keyword_ranks[pid]) if pid in keyword_ranks else 0)
              for pid, rank in vector_ranks.items()}
    ordered = sorted(scores, key=lambda pid: (-scores[pid], -semantic[pid], pid))
    return [{"paper_id": pid, "keyword_score": lexical.get(pid, 0.0),
             "keyword_rank": keyword_ranks.get(pid), "vector_similarity": semantic[pid],
             "vector_rank": vector_ranks[pid], "rrf_score": scores[pid], "hybrid_rank": rank}
            for rank, pid in enumerate(ordered, 1)]


def rank_topic(es, index, topic, query_vectors, profile, filters):
    lexical = {h["_id"]: float(h["_score"]) for h in es.scan(index,
        {"bool": {"must": [lexical_query(topic, profile.get("fields", ["title^6", "abstract^2", "tags.value^3"]))],
                  "filter": filters}})}
    # script_score adds one to make the ES score nonnegative; report actual cosine.
    semantic = {h["_id"]: float(h["_score"]) - 1.0 for h in es.scan(index, vector_query(query_vectors, filters))}
    return fuse_scores(lexical, semantic, profile.get("rrf_k", 60))


def result_mapping():
    return {"settings": {"number_of_shards": 1, "number_of_replicas": 0}, "mappings": {
        "dynamic": False, "properties": {"run_id": {"type": "keyword"}, "paper_id": {"type": "keyword"},
            "snapshot_id": {"type": "keyword"}, "title": {"type": "text"}, "abstract": {"type": "text"},
            "venue": {"type": "keyword"}, "year": {"type": "integer"}, "best_rank": {"type": "integer"},
            "topic_ids": {"type": "keyword"}}}}


def store_run(output, summary, selected, es, hdfs_base):
    from ..storage import HdfsStore
    from ..tool import spark_session
    import getpass

    if not hdfs_base.startswith("/papers/") or ".." in hdfs_base.split("/"):
        raise ValueError("Screening output must be below /papers/")
    root = hdfs_base.rstrip("/") + "/" + summary["run_id"]
    store = HdfsStore()
    store.dfs("-mkdir", "-p", root + "/tables")
    store.dfs("-chown", getpass.getuser(), root + "/tables")
    for name in ("scores.jsonl", "selected.jsonl", "configuration.json"):
        store.put(output / name, root + "/" + name)
    uri = HDFS_URI + root
    schema = ("paper_id STRING, topic_id STRING, keyword_score DOUBLE, keyword_rank INT, "
              "vector_similarity DOUBLE, vector_rank INT, rrf_score DOUBLE, hybrid_rank INT, selected BOOLEAN")
    with spark_session("store-paper-screening") as spark:
        scores = spark.read.schema(schema).option("mode", "FAILFAST").json(uri + "/scores.jsonl")
        scores.write.mode("overwrite").parquet(uri + "/tables/scores")
        scores.createOrReplaceTempView("screening_scores")
        summary["topic_stats"] = [r.asDict() for r in spark.sql("""
            SELECT topic_id, count(*) AS scored, count_if(selected) AS selected,
                   max(rrf_score) AS highest_rrf, min(rrf_score) AS lowest_rrf
            FROM screening_scores GROUP BY topic_id ORDER BY topic_id
        """).collect()]
    if not es.exists(RUN_INDEX):
        es.request("PUT", "/" + RUN_INDEX, {"settings": {"number_of_replicas": 0},
            "mappings": {"dynamic": False, "properties": {
                "run_id": {"type": "keyword"}, "snapshot_id": {"type": "keyword"},
                "created_at": {"type": "date"}, "status": {"type": "keyword"}}}})
    if not es.exists(RESULT_INDEX):
        es.request("PUT", "/" + RESULT_INDEX, result_mapping())
    es.bulk(RESULT_INDEX, ((summary["run_id"] + ":" + row["paper_id"], {**row,
        "run_id": summary["run_id"], "snapshot_id": summary["snapshot_id"],
        "best_rank": min(m["hybrid_rank"] for m in row["retrieval"]["matches"]),
        "topic_ids": [m["topic_id"] for m in row["retrieval"]["matches"]]}) for row in selected))
    summary.update(status="complete", hdfs_root=root)
    es.request("PUT", f'/{RUN_INDEX}/_doc/{summary["run_id"]}?refresh=wait_for', {
        **summary, "selected_ids": [r["paper_id"] for r in selected],
    })
    write_json(output / "summary.json", summary)
    store.put(output / "summary.json", root + "/summary.json")


def screen(index_name, profile, cache_path, output, *, es_url="http://127.0.0.1:9200", per_topic=None,
           venues=None, years=None, hdfs_base="/papers/screening"):
    es = Elasticsearch(es_url)
    index, meta = es.corpus(index_name)
    if vector_signature(profile["embedding"]) != meta["vector_signature"]:
        raise ValueError("Query embedding model/format differs from the corpus index")
    n = per_topic if per_topic is not None else profile.get("per_topic", 0)
    if n < 1:
        raise ValueError("per_topic must be positive")
    filters = filters_for(venues, years)
    count = es.request("POST", f"/{index}/_count", {"query": {"bool": {"filter": filters}}})["count"]
    if not count:
        raise ValueError("No papers in the requested scope")
    material = {"index": index, "snapshot_id": meta["snapshot_id"], "profile": profile,
                "per_topic": n, "venues": venues, "years": years, "method": METHOD}
    run_id = hashlib.sha256(json.dumps(material, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:24]
    output = Path(output)
    if (output / "summary.json").exists():
        old = json.loads((output / "summary.json").read_text())
        if old["run_id"] != run_id:
            raise ValueError("Output belongs to a different screening run")
        if old.get("status") == "complete":
            return load_selection(output)[0]
    output.mkdir(parents=True, exist_ok=True)
    cache = EmbeddingCache(cache_path, profile["embedding"])
    selected_matches = defaultdict(list)
    started = time.monotonic()
    try:
        texts = [query_text(q, profile["embedding"]) for t in profile["topics"] for q in t["queries"]]
        vectors = cache.get_or_create(texts, EmbeddingAPI(profile["embedding"]).encode)
        offset = 0
        def score_records():
            nonlocal offset
            for topic in profile["topics"]:
                end = offset + len(topic["queries"])
                ranked = rank_topic(es, index, topic, vectors[offset:end], profile, filters)
                offset = end
                if len(ranked) != count:
                    raise ValueError(f'Incomplete vector coverage for {topic["id"]}: {len(ranked)}/{count}')
                for row in ranked:
                    row.update(topic_id=topic["id"], selected=row["hybrid_rank"] <= n)
                    if row["selected"]:
                        selected_matches[row["paper_id"]].append(dict(row))
                    yield row
                print(f'{topic["id"]}: scored {len(ranked)}, selected {min(n, len(ranked))}', flush=True)
        write_jsonl(output / "scores.jsonl", score_records())
        ids = sorted(selected_matches, key=lambda pid: (min(r["hybrid_rank"] for r in selected_matches[pid]), pid))
        documents = {doc["paper_id"]: doc for doc in es.documents(index, ids)}
        selected = [{**documents[pid], "retrieval": {"run_id": run_id, "matches": selected_matches[pid]}} for pid in ids]
        write_jsonl(output / "selected.jsonl", selected)
        write_json(output / "configuration.json", material)
        summary = {**material, "run_id": run_id, "kind": "screening-selection", "status": "computed",
                   "created_at": now(), "papers_scored": count, "selected_count": len(selected),
                   "selection_sha256": file_sha256(output / "selected.jsonl"),
                   "configuration_sha256": file_sha256(output / "configuration.json"),
                   "scoring_seconds": round(time.monotonic() - started, 3),
                   "new_query_vectors": cache.new_vectors, "api_tokens": cache.new_tokens}
        store_run(output, summary, selected, es, hdfs_base)
        return summary
    finally:
        cache.close()


def load_selection(directory):
    directory = Path(directory)
    summary = json.loads((directory / "summary.json").read_text())
    if summary.get("kind") != "screening-selection" or summary.get("status") != "complete":
        raise ValueError("PDF ingestion requires a complete screening result")
    if file_sha256(directory / "selected.jsonl") != summary["selection_sha256"]:
        raise ValueError("Selection content differs from its completed screening run")
    if file_sha256(directory / "configuration.json") != summary["configuration_sha256"]:
        raise ValueError("Selection configuration differs from its completed screening run")
    rows = read_jsonl(directory / "selected.jsonl")
    if len(rows) != summary["selected_count"] or len({r["paper_id"] for r in rows}) != len(rows):
        raise ValueError("Selection count/IDs differ from the screening run")
    if any(r.get("retrieval", {}).get("run_id") != summary["run_id"] for r in rows):
        raise ValueError("Selection contains records from another run")
    return summary, rows


def search(index_name, text, profile, cache_path, *, es_url="http://127.0.0.1:9200", run_id=None,
           venues=None, years=None, top=20):
    es = Elasticsearch(es_url)
    ids = None
    if run_id:
        run = es.request("GET", f"/{RUN_INDEX}/_doc/{run_id}")["_source"]
        if run["status"] != "complete":
            raise ValueError("Screening run is incomplete")
        index_name = run["index"]
        ids = run["selected_ids"]
    index, meta = es.corpus(index_name)
    if vector_signature(profile["embedding"]) != meta["vector_signature"]:
        raise ValueError("Query model differs from corpus vectors")
    cache = EmbeddingCache(cache_path, profile["embedding"])
    try:
        vectors = cache.get_or_create([query_text(text, profile["embedding"])], EmbeddingAPI(profile["embedding"]).encode)
        ranking = rank_topic(es, index, {"queries": [text]}, vectors, profile, filters_for(venues, years, ids))[:top]
        if not ranking:
            return {"query": text, "scope_run": run_id, "results": []}
        docs = es.documents(index, [r["paper_id"] for r in ranking])
        return {"query": text, "scope_run": run_id, "index": index,
                "results": [{**doc, "search_scores": rank} for doc, rank in zip(docs, ranking)]}
    finally:
        cache.close()

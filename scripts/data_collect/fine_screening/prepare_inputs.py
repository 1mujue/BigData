"""Build fine-screening inputs from a completed selection and its PDF run."""

import hashlib
import json
import subprocess

from ..retrieval.screening import load_selection
from ..storage import HdfsStore
from ..tool import read_jsonl, write_jsonl
from .public_sources import pdf_text
from .screening import validate_paper


def prepare_inputs(selection_dir, pdf_run, output):
    selection, papers = load_selection(selection_dir)
    pdf_summary = json.loads((pdf_run / "summary.json").read_text(encoding="utf-8"))
    if (pdf_summary["screening_run"] != selection["run_id"]
            or pdf_summary["selection_sha256"] != selection["selection_sha256"]):
        raise ValueError("PDF run belongs to a different coarse selection")
    pdf_rows = read_jsonl(pdf_run / "manifest.jsonl")
    downloads = {row["paper_id"]: row for row in pdf_rows}
    if len(downloads) != len(pdf_rows) or downloads.keys() != {paper["paper_id"] for paper in papers}:
        raise ValueError("PDF manifest IDs do not match the coarse selection")
    if output.exists():
        raise ValueError("Output already exists; choose a new input file")

    store = HdfsStore()
    issues = []

    def rows():
        for paper in papers:
            pid = paper["paper_id"]
            download = downloads[pid]
            sources = []
            if download["status"] == "stored":
                try:
                    body = store.dfs("-cat", download["hdfs_path"], decode=False)
                    if hashlib.sha256(body).hexdigest() != download["sha256"]:
                        raise ValueError("PDF hash differs from the download manifest")
                    sources.append({"source_id": "paper", "kind": "paper",
                                    "url": download.get("source_url"), "text": pdf_text(body)})
                except (OSError, ValueError, subprocess.CalledProcessError) as error:
                    issues.append({"paper_id": pid, "reason": str(error)})
            else:
                issues.append({"paper_id": pid, "reason": download.get("reason") or download["status"]})

            row = {key: paper[key] for key in ("paper_id", "title", "venue", "year", "paper_url", "abstract", "pdf_urls")}
            row["tags"] = list(dict.fromkeys(tag["value"] for tag in paper["tags"]))
            row["source_eligible"] = True if paper.get("scope", {}).get("status") == "published" else None
            row["sources"] = sources
            validate_paper(row)
            yield row

    write_jsonl(output, rows())
    return {"papers": len(papers), "with_paper_text": len(papers) - len(issues), "missing_text": issues}

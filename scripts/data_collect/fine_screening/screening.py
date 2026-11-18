"""Prepare self-contained reading batches and validate source-grounded results."""
import copy
from collections import Counter
import hashlib
import json
from pathlib import Path
import unicodedata
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
RUBRIC_FILE = ROOT.parent / "config/fine_screening_rubric.json"
TIERS = {"priority": 0, "possible": 1, "needs_evidence": 2, "defer": 3}
SCOPES = {"metadata_only", "partial_text", "paper_text", "paper_and_repository"}
KINDS = {"metadata", "paper", "repository", "data", "model", "prior_note"}
SUPPORTS = {"main_content", "in_scope", "training", "benchmark_primary", "implementation", "extracted", "A", "B", "C", "D"}
COMMON = {"paper_id", "stage", "model", "reading_scope", "main_content", "reason", "evidence", "extra_sources", "next_checks"}
DETAIL = {"extracted", "in_scope", "training", "benchmark_primary", "implementation", "feasibility_note", "criteria"}
SHARED_CONTRACT_FILES = (
    "docs/standard.md", "docs/records.md",
    "prompts/triage.md", "prompts/detail.md", "prompts/research.md",
)
CONTRACT_FILES = (*SHARED_CONTRACT_FILES, "rubric.json", "batch.json", "docs/batch.md")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def text(value, label, nullable=False):
    require((nullable and value is None) or (isinstance(value, str) and bool(value.strip())), f"{label}: expected nonempty text" + (" or null" if nullable else ""))


def strings(value, label, nullable=False):
    if nullable and value is None:
        return
    require(isinstance(value, list), f"{label}: expected string array")
    for item in value:
        text(item, label)


def keys(value, expected, label):
    require(isinstance(value, dict), f"{label}: expected object")
    missing, extra = expected - value.keys(), value.keys() - expected
    require(not missing and not extra, f"{label}: missing {sorted(missing)}, unexpected {sorted(extra)}")


def nullable_bool(value, label):
    require(value is None or type(value) is bool, f"{label}: expected true, false or null")


def choice(value, allowed, label):
    require(isinstance(value, str) and value in allowed, "invalid " + label)


def url(value, label):
    if value is None:
        return
    text(value, label)
    parts = urlsplit(value)
    require(parts.scheme in {"http", "https"} and bool(parts.netloc) and parts.username is None and parts.password is None,
            f"{label}: expected public HTTP(S) URL without credentials")


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def iter_rows(path):
    with Path(path).open("rb") as source:
        for number, line in enumerate(source, 1):
            if line.strip():
                try:
                    yield number, json.loads(line.decode("utf-8"))
                except ValueError as error:
                    raise ValueError(f"{Path(path).name}:{number}: {error}") from error


def read_rows(path):
    return [row for _, row in iter_rows(path)]


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def write_rows(path, rows):
    Path(path).write_text("".join(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n" for row in rows), encoding="utf-8")


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def norm(value):
    return " ".join(unicodedata.normalize("NFKC", value).translate(str.maketrans({"“": '"', "”": '"', "‘": "'", "’": "'"})).split())


def validate_rubric(rubric):
    keys(rubric, {"version", "weights", "criteria"}, "rubric")
    text(rubric["version"], "rubric.version")
    for field in ("weights", "criteria"):
        keys(rubric[field], set("ABCD"), "rubric." + field)
    for key in "ABCD":
        require(type(rubric["weights"][key]) is int and rubric["weights"][key] > 0, f"invalid weight {key}")
        keys(rubric["criteria"][key], {"name", "levels"}, key)
        text(rubric["criteria"][key]["name"], key)
        keys(rubric["criteria"][key]["levels"], set("0123"), key + ".levels")
        for value in rubric["criteria"][key]["levels"].values():
            text(value, key + ".levels")


def validate_contract(rubric, batch):
    validate_rubric(rubric)
    keys(batch, {"id", "scope", "max_concurrency", "require_no_new_training", "exclude_benchmark_primary"}, "batch")
    text(batch["id"], "batch.id")
    text(batch["scope"], "batch.scope")
    require(type(batch["max_concurrency"]) is int and batch["max_concurrency"] > 0, "max_concurrency must be a positive integer")
    for key in ("require_no_new_training", "exclude_benchmark_primary"):
        require(type(batch[key]) is bool, key + " must be boolean")


def render_batch(batch):
    training = ("Defer experiments that require new model parameter training."
                if batch["require_no_new_training"] else "New model parameter training is allowed; assess the actual preparation work.")
    benchmark = ("Defer papers whose main contribution is a new benchmark."
                 if batch["exclude_benchmark_primary"] else "Papers whose main contribution is a new benchmark are eligible for assessment.")
    return f"# Batch scope\n\n{batch['scope']}\n\n## Eligibility conditions\n\n- {training}\n- {benchmark}\n"


def render_rubric(rubric):
    weights = rubric["weights"]
    formula = " + ".join(f"{weights[k]}{k}" for k in "ABCD")
    lines = ["# 四维评分", "", f"版本：`{rubric['version']}`。", "", f"总分为 `S = ({formula}) / {3 * sum(weights.values())}`，范围为 0 到 1，展示两位小数，排序使用完整精度。等级取 0、1、2、3；证据不足时取 `null`。任一等级为 `null`，总分为 `null`。", "", "| 维度 | 3 分 | 2 分 | 1 分 | 0 分 |", "| --- | --- | --- | --- | --- |"]
    for key in "ABCD":
        item = rubric["criteria"][key]
        lines.append("| " + " | ".join([key + "：" + item["name"], *(item["levels"][str(n)] for n in (3, 2, 1, 0))]) + " |")
    lines += ["", "先核对本批适配条件与证据准备情况，再分档。明确不符合条件或存在 0 分障碍时为暂缓；影响判断的未知项为待补查。其余记录中，四项均至少 2 分为优先，存在 1 分为可选。", "", "优先、可选分别按总分降序排列；同分用论文标识稳定排序。这个顺序辅助阅读和选择，最终名单另行给出选择理由。", ""]
    return "\n".join(lines)


def source_map(paper, extras):
    require(isinstance(paper.get("sources", []), list) and isinstance(extras, list), "sources must be arrays")
    metadata = "Title: " + paper["title"] + "\nAbstract: " + (paper.get("abstract") or "") + "\nTags: " + ", ".join(paper.get("tags", []))
    sources = {"metadata": {"source_id": "metadata", "kind": "metadata", "url": paper.get("paper_url"), "text": metadata}}
    for source in [*paper.get("sources", []), *extras]:
        keys(source, {"source_id", "kind", "url", "text"}, "source")
        text(source["source_id"], "source_id")
        text(source["text"], "source.text")
        choice(source["kind"], KINDS, "source kind")
        url(source["url"], "source.url")
        require(source["source_id"] not in sources, "duplicate source_id: " + source["source_id"])
        sources[source["source_id"]] = source
    return sources


def validate_paper(paper):
    required = {"paper_id", "title", "venue", "year", "paper_url", "abstract", "tags", "source_eligible", "sources"}
    require(isinstance(paper, dict) and required <= paper.keys(), "paper: required metadata fields are missing")
    require(not (paper.keys() - required - {"pdf_urls"}), "paper: unexpected fields; supply original metadata and sources only")
    for key in ("paper_id", "title", "venue"):
        text(paper[key], key)
    require(type(paper["year"]) is int and 1000 <= paper["year"] <= 9999, "year must be a four-digit integer")
    url(paper["paper_url"], "paper_url")
    require(paper["abstract"] is None or isinstance(paper["abstract"], str), "abstract must be text or null")
    strings(paper["tags"], "tags")
    nullable_bool(paper["source_eligible"], "source_eligible")
    if "pdf_urls" in paper:
        strings(paper["pdf_urls"], "pdf_urls")
        for value in paper["pdf_urls"]:
            url(value, "pdf_urls")
    source_map(paper, [])


def evaluate(paper, record, stage, rubric, batch):
    """Validate a supplied judgment; derive arithmetic and eligibility without a model call."""
    validate_paper(paper)
    validate_contract(rubric, batch)
    require(stage in {"triage", "detail"}, "invalid stage")
    keys(record, COMMON | ({"decision"} if stage == "triage" else DETAIL), "record")
    require(record["paper_id"] == paper["paper_id"], "paper_id does not match assigned input")
    require(record["stage"] == stage, "stage does not match assignment")
    for key in ("model", "main_content", "reason"):
        text(record[key], key)
    choice(record["reading_scope"], SCOPES, "reading_scope")
    sources = source_map(paper, record["extra_sources"])
    require(isinstance(record["evidence"], list) and bool(record["evidence"]), "evidence must not be empty")
    supported, paper_support = set(), set()
    for entry in record["evidence"]:
        keys(entry, {"source_id", "locator", "quote", "supports"}, "evidence")
        text(entry["source_id"], "evidence.source_id")
        require(entry["source_id"] in sources, "evidence source_id is not registered")
        require(stage != "triage" or entry["source_id"] == "metadata", "triage must cite metadata only")
        require(sources[entry["source_id"]]["kind"] != "prior_note", "prior_note is a navigation aid, not primary evidence")
        text(entry["locator"], "evidence.locator")
        text(entry["quote"], "evidence.quote")
        strings(entry["supports"], "evidence.supports")
        require(entry["supports"] and set(entry["supports"]) <= SUPPORTS, "invalid evidence.supports")
        require(norm(entry["quote"]) in norm(sources[entry["source_id"]]["text"]), "quote absent from registered source")
        supported.update(entry["supports"])
        if sources[entry["source_id"]]["kind"] == "paper":
            paper_support.update(entry["supports"])
    require("main_content" in supported, "main_content needs evidence")
    require(isinstance(record["next_checks"], list), "next_checks must be an array")
    for check in record["next_checks"]:
        keys(check, {"question", "blocking"}, "next_checks entry")
        text(check["question"], "next_checks.question")
        require(type(check["blocking"]) is bool, "next_checks.blocking must be boolean")
    known_failures, missing = [], []
    if paper["source_eligible"] is False:
        known_failures.append("source_ineligible")
    elif paper["source_eligible"] is None:
        missing.append("source_qualification_unknown")
    if any(item["blocking"] for item in record["next_checks"]):
        missing.append("blocking_check")
    score = None
    if stage == "triage":
        require(not record["extra_sources"], "triage does not accept additional sources")
        require(record["reading_scope"] == "metadata_only", "triage uses metadata_only reading_scope")
        choice(record["decision"], TIERS, "triage decision")
        if record["decision"] == "defer":
            known_failures.append("triage_defer")
        tier = "defer" if known_failures else "needs_evidence" if missing else record["decision"]
    else:
        extracted = record["extracted"]
        keys(extracted, {"topics", "contribution_types", "method", "models", "datasets", "metrics"}, "extracted")
        for key in ("topics", "contribution_types", "models", "datasets", "metrics"):
            strings(extracted[key], "extracted." + key, nullable=True)
        text(extracted["method"], "extracted.method", nullable=True)
        text(record["feasibility_note"], "feasibility_note", nullable=True)
        nullable_bool(record["in_scope"], "in_scope")
        nullable_bool(record["benchmark_primary"], "benchmark_primary")
        choice(record["training"], {"none", "required", "unknown"}, "training")
        for key in ("in_scope", "training", "benchmark_primary"):
            value = record[key]
            if value is not None and value != "unknown":
                require(key in supported, key + " needs evidence")
        if record["in_scope"] is False:
            known_failures.append("out_of_scope")
        elif record["in_scope"] is None:
            missing.append("scope_unknown")
        if batch["require_no_new_training"]:
            if record["training"] == "required":
                known_failures.append("new_training_required")
            elif record["training"] == "unknown":
                missing.append("training_unknown")
        if batch["exclude_benchmark_primary"]:
            if record["benchmark_primary"] is True:
                known_failures.append("benchmark_primary")
            elif record["benchmark_primary"] is None:
                missing.append("contribution_unknown")
        implementation = record["implementation"]
        keys(implementation, {"url", "status", "route", "preparation"}, "implementation")
        url(implementation["url"], "implementation.url")
        choice(implementation["status"], {"inspected", "linked", "claimed", "not_found", "unknown"}, "implementation.status")
        choice(implementation["route"], {"author_code", "paper_based", "mixed", "unknown"}, "implementation.route")
        text(implementation["preparation"], "implementation.preparation", nullable=True)
        if implementation["status"] in {"inspected", "linked"}:
            require(implementation["url"] is not None, "inspected/linked implementation needs a URL")
        if implementation["route"] != "unknown":
            require("implementation" in supported, "implementation route needs evidence")
        if implementation["route"] == "unknown":
            missing.append("implementation_route_unknown")
        elif implementation["route"] in {"author_code", "mixed"} and implementation["status"] not in {"inspected", "linked"}:
            missing.append("author_code_not_established")
        keys(record["criteria"], set("ABCD"), "criteria")
        levels = {}
        for key, item in record["criteria"].items():
            keys(item, {"level", "reason"}, "criterion " + key)
            value = item["level"]
            require(value is None or (type(value) is int and 0 <= value <= 3), "invalid level " + key)
            text(item["reason"], "criterion reason " + key)
            if value is not None:
                require(key in supported, "criterion " + key + " needs evidence")
            levels[key] = value
        require(record["reading_scope"] != "metadata_only" or all(v is None for v in levels.values()), "metadata_only records cannot receive numerical levels")
        if all(v is not None for v in levels.values()):
            score = sum(rubric["weights"][k] * levels[k] for k in "ABCD") / (3 * sum(rubric["weights"].values()))
        else:
            missing.append("unrated_dimensions")
        if any(v == 0 for v in levels.values()):
            known_failures.append("zero_dimension")
        if record["reading_scope"] not in {"paper_text", "paper_and_repository"}:
            missing.append("body_reading_incomplete")
        if not paper_support:
            missing.append("body_evidence_missing")
        if known_failures:
            tier = "defer"
        elif missing:
            tier = "needs_evidence"
        else:
            tier = "priority" if min(levels.values()) >= 2 else "possible"
    inventory = [{"source_id": key, "kind": s["kind"], "url": s["url"],
                  "sha256": hashlib.sha256(s["text"].encode()).hexdigest()} for key, s in sources.items()]
    return {"paper_id": paper["paper_id"], "title": paper["title"], "venue": paper["venue"], "year": paper["year"],
            "paper_url": paper["paper_url"], "source_eligible": paper["source_eligible"],
            "assessment": record, "source_inventory": inventory, "rubric_version": rubric["version"], "batch_id": batch["id"],
            "tier": tier, "score": score, "rankable": stage == "detail" and tier in {"priority", "possible"},
            "status_reasons": known_failures + missing}


def new_directory(path):
    path = Path(path)
    require(not path.exists(), "output path already exists; choose a new directory")
    path.mkdir(parents=True)
    return path


def object_digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()).hexdigest()


def validate_reader(reader):
    choice(reader.get("mode"), {"codex", "api"}, "reader mode")
    text(reader.get("model"), "reader.model")
    text(reader.get("effort"), "reader.effort")
    if reader["mode"] == "api":
        choice(reader.get("provider"), {"deepseek", "openai"}, "API provider")
        choice(reader.get("detail_sources", "supplied"), {"web", "supplied"}, "reader.detail_sources")
        url(reader.get("base_url"), "reader.base_url")
        text(reader.get("base_url"), "reader.base_url")
        text(reader.get("key_env"), "reader.key_env")


def prepare(input_path, output, stage, *, batch_path, readers_path, reader="codex"):
    settings = load_json(readers_path)
    require(reader in settings, "reader configuration is missing")
    selected_reader = settings[reader] | {"mode": reader}
    validate_reader(selected_reader)
    input_path = Path(input_path).resolve()
    tasks, ids = [], set()
    for number, paper in iter_rows(input_path):
        validate_paper(paper)
        require(paper["paper_id"] not in ids, "duplicate input paper_id")
        ids.add(paper["paper_id"])
        tasks.append({"task_id": f"{len(tasks) + 1:06d}", "paper_id": paper["paper_id"],
                      "line": number, "input_sha256": object_digest(paper)})
    require(tasks, "input has no papers")
    contents = {name: (ROOT / name).read_text(encoding="utf-8") for name in SHARED_CONTRACT_FILES}
    contents["rubric.json"] = RUBRIC_FILE.read_text(encoding="utf-8")
    rubric, batch = json.loads(contents["rubric.json"]), load_json(batch_path)
    validate_contract(rubric, batch)
    contents["batch.json"] = json.dumps(batch, ensure_ascii=False, indent=2) + "\n"
    contents["docs/batch.md"] = render_batch(batch)
    prompt = "\n\n".join([
        contents[f"prompts/{stage}.md"], "# General standard\n" + contents["docs/standard.md"],
        "# Rubric\n" + render_rubric(rubric), "# Current batch\n" + contents["docs/batch.md"],
        "# Record contract\n" + contents["docs/records.md"],
    ])
    out = new_directory(output)
    hashes = {}
    for name, content in contents.items():
        path = out / "contract" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        hashes[name] = digest(path)
    (out / "prompt.md").write_text(prompt, encoding="utf-8")
    write_json(out / "reader.json", selected_reader)
    manifest = {"format_version": 3, "stage": stage, "input_path": str(input_path),
                "contract": hashes, "reader_sha256": digest(out / "reader.json"),
                "prompt_sha256": digest(out / "prompt.md"), "tasks": tasks}
    write_json(out / "manifest.json", manifest)
    return {"stage": stage, "papers": len(tasks), "max_concurrency": batch["max_concurrency"]}


def load_run(run):
    run = Path(run)
    manifest = load_json(run / "manifest.json")
    keys(manifest, {"format_version", "stage", "input_path", "contract", "reader_sha256", "prompt_sha256", "tasks"}, "manifest")
    require(manifest["format_version"] == 3 and manifest["stage"] in {"triage", "detail"}, "unsupported run format/stage")
    require(digest(run / "reader.json") == manifest["reader_sha256"], "frozen reader configuration changed")
    validate_reader(load_json(run / "reader.json"))
    keys(manifest["contract"], set(CONTRACT_FILES), "manifest.contract")
    for name, sha in manifest["contract"].items():
        require(digest(run / "contract" / name) == sha, "frozen contract changed: " + name)
    require(digest(run / "prompt.md") == manifest["prompt_sha256"], "frozen prompt changed")
    rubric, batch = load_json(run / "contract/rubric.json"), load_json(run / "contract/batch.json")
    validate_contract(rubric, batch)
    require(isinstance(manifest["tasks"], list) and manifest["tasks"], "manifest has no tasks")
    ids = set()
    for index, task in enumerate(manifest["tasks"], 1):
        keys(task, {"task_id", "paper_id", "line", "input_sha256"}, "task")
        require(task["task_id"] == f"{index:06d}", "invalid task_id")
        text(task["paper_id"], "paper_id")
        require(task["paper_id"] not in ids, "duplicate assigned paper_id")
        ids.add(task["paper_id"])
    return manifest, rubric, batch


def assigned_papers(manifest):
    tasks = {t["line"]: t for t in manifest["tasks"]}
    seen = set()
    for line, paper in iter_rows(manifest["input_path"]):
        require(line in tasks, "input contains an unassigned row")
        task = tasks[line]
        validate_paper(paper)
        require(paper["paper_id"] == task["paper_id"] and object_digest(paper) == task["input_sha256"],
                "assigned input changed: " + task["paper_id"])
        seen.add(line)
        yield task, paper
    require(seen == set(tasks), "assigned input is missing rows")


def resolve_record(record, work):
    record = copy.deepcopy(record)
    require(isinstance(record, dict), "record must be an object")
    require(isinstance(record.get("extra_sources"), list), "extra_sources must be an array")
    for source in record["extra_sources"]:
        keys(source, {"source_id", "kind", "url", "path"}, "additional source")
        path = Path(work) / source.pop("path")
        source["text"] = path.read_text(encoding="utf-8")
    return record


def read_work_record(work, reader):
    rows = read_rows(Path(work) / "results.jsonl")
    require(len(rows) == 1, "expected exactly one result for this paper")
    record = rows[0]
    require(isinstance(record, dict), "record must be an object")
    record["model"] = reader["model"]
    return record


def check_work(work):
    work = Path(work)
    assignment = load_json(work / "assignment.json")
    batch = load_json(work / "batch.json")
    record = resolve_record(read_work_record(work, load_json(work / "reader.json")), work)
    evaluate(load_json(work / "input.json"), record, assignment["stage"],
             load_json(work / "rubric.json"), batch)
    return {"valid": True}


def save_record(run, task, paper, record, work, rubric, batch, stage, attempt):
    evaluated = evaluate(paper, resolve_record(record, work), stage, rubric, batch)
    evaluated["assessment"].pop("extra_sources")
    evaluated["task_id"] = task["task_id"]
    evaluated["input_sha256"] = task["input_sha256"]
    evaluated["attempt"] = str(Path(attempt).relative_to(run))
    path = Path(run) / "records" / (task["task_id"] + ".json")
    require(not path.exists(), "a validated record already exists for this paper")
    path.parent.mkdir(exist_ok=True)
    write_json(path, evaluated)
    write_json(Path(attempt) / "record.json", {"sha256": digest(path)})
    return evaluated


def collect_records(run):
    run = Path(run)
    manifest, rubric, batch = load_run(run)
    records, errors, missing, failures = [], [], [], []
    expected_paths = {t["task_id"] + ".json" for t in manifest["tasks"]}
    if (run / "records").exists():
        require({p.name for p in (run / "records").iterdir()} <= expected_paths, "unassigned retained record")
    for task in manifest["tasks"]:
        path = run / "records" / (task["task_id"] + ".json")
        if not path.exists():
            missing.append(task["paper_id"])
            for status in sorted((run / "attempts" / task["task_id"]).glob("*/status.json")):
                failures.append(load_json(status))
            continue
        try:
            row = load_json(path)
            require(row["paper_id"] == task["paper_id"] and row["task_id"] == task["task_id"], "retained paper_id differs")
            require(row["input_sha256"] == task["input_sha256"], "retained input identity differs")
            require(row["rubric_version"] == rubric["version"] and row["batch_id"] == batch["id"], "retained contract differs")
            require(row["assessment"]["stage"] == manifest["stage"], "retained stage differs")
            attempt = (run / row["attempt"]).resolve()
            require(attempt.parent == (run / "attempts" / task["task_id"]).resolve(), "retained attempt differs")
            require(digest(path) == load_json(attempt / "record.json")["sha256"], "retained record changed after validation")
            records.append(row)
        except (ValueError, OSError, KeyError, TypeError) as error:
            errors.append({"paper_id": task["paper_id"], "error": str(error)})
    summary = {"stage": manifest["stage"], "expected": len(manifest["tasks"]), "valid": len(records),
               "missing": missing, "errors": errors, "execution_failures": failures,
               "complete": not missing and not errors, "tiers": dict(Counter(r["tier"] for r in records)),
               "rubric_version": rubric["version"], "batch_id": batch["id"], "run_manifest_sha256": digest(run / "manifest.json"),
               "reader_mode": load_json(run / "reader.json")["mode"],
               "requested_reader_model": load_json(run / "reader.json")["model"],
               "reader_models": dict(Counter(r["assessment"]["model"] for r in records))}
    return records, summary


def analysis_row(row):
    raw = row["assessment"]
    return {**{k: row[k] for k in ("paper_id", "title", "venue", "year", "tier", "score", "rankable", "source_eligible", "rubric_version", "batch_id")},
            "stage": raw["stage"], "reading_scope": raw["reading_scope"], "model": raw["model"],
            "training": raw.get("training"), "benchmark_primary": raw.get("benchmark_primary"), "in_scope": raw.get("in_scope"),
            "implementation_status": raw.get("implementation", {}).get("status"),
            **{k: raw.get("criteria", {}).get(k, {}).get("level") for k in "ABCD"},
            **{k: raw.get("extracted", {}).get(k) for k in ("topics", "contribution_types", "models", "datasets", "metrics")},
            "next_checks": [c["question"] for c in raw["next_checks"]], "status_reasons": row["status_reasons"]}


def complete_records(run):
    records, summary = collect_records(run)
    require(summary["complete"], "run is incomplete or invalid; use check for details")
    return records, summary


def collect(run, output):
    records, summary = complete_records(run)
    ranked = sorted((r for r in records if r["rankable"]), key=lambda r: (TIERS[r["tier"]], -r["score"], r["paper_id"]))
    out = new_directory(output)
    write_rows(out / "assessments.jsonl", records)
    write_rows(out / "ranked.jsonl", ranked)
    for tier in ("needs_evidence", "defer"):
        write_rows(out / (tier + ".jsonl"), [r for r in records if r["tier"] == tier])
    write_rows(out / "analysis.jsonl", [analysis_row(r) for r in records])
    write_json(out / "summary.json", summary)
    return summary


def export_selection(run, selection_path, output):
    records, summary = complete_records(run)
    require(summary["stage"] == "detail", "final selection requires detailed readings")
    selection = load_json(selection_path)
    require(isinstance(selection, list) and selection, "selection must be a nonempty array")
    by_id, selected, seen = {r["paper_id"]: r for r in records}, [], set()
    for item in selection:
        keys(item, {"paper_id", "reason"}, "selection entry")
        text(item["paper_id"], "selection paper_id")
        text(item["reason"], "selection reason")
        require(item["paper_id"] in by_id and item["paper_id"] not in seen, "unknown or duplicate selected paper_id")
        row = by_id[item["paper_id"]]
        require(row["rankable"], "selected paper is not eligible for the recommendation list: " + item["paper_id"])
        seen.add(item["paper_id"])
        selected.append(row | {"selection_order": len(selected) + 1, "selection_reason": item["reason"]})
    out = new_directory(output)
    write_rows(out / "recommendations.jsonl", selected)
    write_json(out / "selection.json", selection)
    lines = ["# 复现候选名单", "", "以下为选题记录，尚不代表复现实验已执行。", ""]
    for row in selected:
        raw = row["assessment"]
        implementation = raw["implementation"]
        route = {"author_code": "依托作者代码", "paper_based": "依据论文描述实现", "mixed": "作者代码与补充实现结合", "unknown": "尚未明确"}[implementation["route"]]
        status = {"inspected": "已检查相关实现", "linked": "仅确认入口可访问", "claimed": "发布承诺，尚未核实可用实现", "not_found": "本次未找到作者实现", "unknown": "尚未核查"}[implementation["status"]]
        lines += [f"## {row['selection_order']}. {row['title']}", "", f"{row['venue']} {row['year']}；{row['tier']}；总分 {row['score']:.2f}。", "",
                  raw["main_content"], "", "选择依据：" + row["selection_reason"], "",
                  "可行性说明：" + (raw["feasibility_note"] or raw["reason"]), "",
                  "实现路线：" + route + "。", "", "作者材料状态：" + status + "。", ""]
        if implementation["url"]:
            lines += ["材料链接：" + implementation["url"], ""]
        lines += [
                  "论文：" + (row["paper_url"] or row["paper_id"]), ""]
    (out / "recommendations.md").write_text("\n".join(lines), encoding="utf-8")
    return {"selected": len(selected), "selection_origin": "explicit_coordinator_selection", "rubric_version": summary["rubric_version"]}

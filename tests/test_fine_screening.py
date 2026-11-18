"""Synthetic contract cases. No paper is read or scored by a model here."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from scripts.data_collect.fine_screening import screening

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures/fine_screening"
PREPARE_CONFIG = {"batch_path": FIXTURES / "batch.json", "readers_path": FIXTURES / "readers.json"}


def sample(levels=(3, 3, 3, 3), paper_id="synthetic-1"):
    text = "This fictional study compares a frozen model with and without feedback. Public inputs and an implementation are available."
    paper = {
        "paper_id": paper_id, "title": "Synthetic contract example", "venue": "EXAMPLE",
        "year": 2025, "paper_url": "https://example.org/paper", "abstract": text,
        "tags": ["coding"], "source_eligible": True,
        "sources": [{"source_id": "paper", "kind": "paper", "url": "https://example.org/paper", "text": text}],
    }
    record = {
        "paper_id": paper_id, "stage": "detail", "model": "synthetic-fixture",
        "reading_scope": "paper_text", "main_content": "Fictional feedback comparison.",
        "reason": "Synthetic case for testing the documented selection contract.",
        "extracted": {"topics": ["coding"], "contribution_types": ["method"],
                      "method": "Feedback", "models": ["Frozen model"],
                      "datasets": ["Synthetic inputs"], "metrics": ["Correctness"]},
        "in_scope": True, "training": "none", "benchmark_primary": False,
        "implementation": {"url": None, "status": "not_found", "route": "paper_based",
                           "preparation": "A small wrapper described by the fictional text."},
        "feasibility_note": "The main comparison has a plausible frozen-model verification route.",
        "criteria": {k: {"level": v, "reason": "Synthetic level supplied by the test, not a model judgment."}
                     for k, v in zip("ABCD", levels)},
        "evidence": [{"source_id": "paper", "locator": "Fictional section 1", "quote": text,
                      "supports": ["main_content", "in_scope", "training", "benchmark_primary", "implementation", "A", "B", "C", "D"]}],
        "extra_sources": [], "next_checks": [],
    }
    return paper, record


class Decisions(unittest.TestCase):
    def setUp(self):
        self.rubric = screening.load_json(screening.RUBRIC_FILE)
        self.batch = screening.load_json(FIXTURES / "batch.json")

    def assess(self, paper, record):
        return screening.evaluate(paper, record, "detail", self.rubric, self.batch)

    def test_maximum_and_priority(self):
        result = self.assess(*sample())
        self.assertEqual((result["score"], result["tier"]), (1.0, "priority"))

    def test_heavy_preparation_is_possible_even_with_high_total(self):
        result = self.assess(*sample((3, 3, 3, 1)))
        self.assertEqual((result["score"], result["tier"]), (32 / 36, "possible"))

    def test_unknown_is_not_zero(self):
        paper, record = sample((3, 3, None, 3))
        record["next_checks"] = [{"question": "Establish the required data access.", "blocking": True}]
        result = self.assess(paper, record)
        self.assertIsNone(result["score"])
        self.assertEqual(result["tier"], "needs_evidence")

    def test_confirmed_zero_defers_even_with_other_unknowns(self):
        result = self.assess(*sample((3, 0, None, 3)))
        self.assertEqual(result["tier"], "defer")
        self.assertIsNone(result["score"])

    def test_known_batch_failure_defers_even_with_unknown_levels(self):
        paper, record = sample((None, None, None, None))
        record["benchmark_primary"] = True
        self.assertEqual(self.assess(paper, record)["tier"], "defer")

    def test_training_is_a_batch_condition_not_a_numeric_bonus(self):
        paper, record = sample()
        record["training"] = "required"
        self.assertEqual(self.assess(paper, record)["tier"], "defer")
        self.batch["require_no_new_training"] = False
        self.assertEqual(self.assess(paper, record)["tier"], "priority")

    def test_a_blocking_source_conflict_stays_pending(self):
        paper, record = sample()
        record["next_checks"] = [{"question": "Resolve a contradictory publication record.", "blocking": True}]
        self.assertEqual(self.assess(paper, record)["tier"], "needs_evidence")

    def test_forged_quote_is_rejected(self):
        paper, record = sample()
        record["evidence"][0]["quote"] = "This sentence is absent from the supplied source."
        with self.assertRaisesRegex(ValueError, "quote"):
            self.assess(paper, record)

    def test_age_or_shared_benchmark_does_not_change_numeric_score(self):
        paper, record = sample()
        old = self.assess(paper, record)
        paper["year"] = 2024
        record["extracted"]["datasets"] = ["HumanEval"]
        new = self.assess(paper, record)
        self.assertEqual((old["score"], old["tier"]), (new["score"], new["tier"]))


class CommandLine(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)

    def cli(self, *args, ok=True):
        result = subprocess.run([sys.executable, "-m", "scripts.data_collect", "fine-screen", *map(str, args)],
                                cwd=ROOT, text=True, capture_output=True)
        self.assertEqual(result.returncode == 0, ok, result.stderr + result.stdout)
        return result

    def make_run(self, pairs=None, stage="detail", name="run", batch_path=FIXTURES / "batch.json"):
        pairs = pairs or [sample((2, 2, 2, 2), "priority-low"), sample((3, 3, 3, 1), "possible-high")]
        inputs = self.base / (name + ".jsonl")
        screening.write_rows(inputs, [p for p, _ in pairs])
        run = self.base / name
        self.cli("prepare", "--stage", stage, "--input", inputs, "--out", run,
                 "--batch", batch_path, "--readers", FIXTURES / "readers.json")
        manifest, rubric, batch = screening.load_run(run)
        for task, (paper, record) in zip(manifest["tasks"], pairs):
            attempt = run / "attempts" / task["task_id"] / "001"
            attempt.mkdir(parents=True)
            screening.save_record(run, task, paper, record, self.base, rubric, batch, stage, attempt)
        return run, manifest

    def test_prepare_collect_export_use_the_actual_entry_point(self):
        run, _ = self.make_run()
        self.cli("check", "--run", run)
        out = self.base / "collected"
        self.cli("collect", "--run", run, "--out", out)
        ranked = screening.read_rows(out / "ranked.jsonl")
        self.assertEqual([x["paper_id"] for x in ranked], ["priority-low", "possible-high"])
        self.assertEqual([x["score"] for x in ranked], [24 / 36, 32 / 36])
        selection = self.base / "selection.json"
        screening.write_json(selection, [{"paper_id": "possible-high", "reason": "Explicit composition tradeoff."}])
        self.cli("export", "--run", run, "--selection", selection, "--out", self.base / "chosen")
        self.assertIn("0.89", (self.base / "chosen/recommendations.md").read_text())

    def test_triage_stays_unscored_and_cannot_be_exported(self):
        papers = screening.read_rows(FIXTURES / 'papers.jsonl')
        records = screening.read_rows(FIXTURES / 'triage.jsonl')
        run, _ = self.make_run(list(zip(papers, records)), stage='triage')
        self.cli('collect', '--run', run, '--out', self.base / 'triage-output')
        rows = screening.read_rows(self.base / 'triage-output/assessments.jsonl')
        self.assertTrue(all(r['score'] is None for r in rows))
        self.assertEqual((self.base / 'triage-output/ranked.jsonl').read_text(), '')
        self.cli('export', '--run', run, '--selection', FIXTURES / 'selection.json', '--out', self.base / 'export', ok=False)

    def test_batch_flags_control_both_prompt_and_eligibility(self):
        batch = screening.load_json(FIXTURES / "batch.json")
        batch.update(scope="Fictional benchmark design.", require_no_new_training=False, exclude_benchmark_primary=False)
        config = self.base / "batch.json"
        screening.write_json(config, batch)
        paper, record = sample()
        record.update(training="required", benchmark_primary=True)
        run, _ = self.make_run([(paper, record)], batch_path=config)
        self.assertEqual(screening.load_json(run / "contract/batch.json"), batch)
        policy = (run / "contract/docs/batch.md").read_text()
        self.assertIn(batch["scope"], policy)
        self.assertIn(policy, (run / "prompt.md").read_text())
        self.assertEqual(screening.complete_records(run)[0][0]["tier"], "priority")


if __name__ == '__main__':
    unittest.main()

import json
import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "generate-pipeline-versioning-submission.py"


def run(*args: str, cwd: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args, cwd=cwd, env=env, check=False, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True,
    )


class GeneratePipelineVersioningSubmissionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        run("git", "init", "-b", "main", cwd=self.root)
        run("git", "config", "user.name", "Test Learner", cwd=self.root)
        run("git", "config", "user.email", "learner@example.com", cwd=self.root)
        run(
            "git", "remote", "add", "origin",
            "https://github.com/test-learner/lab-pipeline-versioning.git", cwd=self.root,
        )
        self._write_revision()
        run("git", "add", ".", cwd=self.root)
        run("git", "commit", "-m", "complete pipeline lab", cwd=self.root)
        self._write_evidence()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _write(self, relative: str, content: str) -> None:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(content).lstrip(), encoding="utf-8")

    def _write_revision(self) -> None:
        self._write(
            "dvc.yaml",
            """
            stages:
              preprocess:
                cmd: python3 scripts/preprocess.py
              train:
                cmd: python3 scripts/train.py
              evaluate:
                cmd: python3 scripts/evaluate.py
            """,
        )
        self._write("dvc.lock", "schema: '2.0'\nstages: {}\n")
        self._write(
            "params.yaml",
            "preprocess:\n  test_split: 0.2\ntrain:\n  n_estimators: 150\n  max_depth: 12\n",
        )
        for name in ("preprocess", "train", "evaluate", "augment_data"):
            self._write(f"scripts/{name}.py", f"print('{name}')\n")

    def _write_evidence(self) -> None:
        self._write(
            "evidence/dvc-exp-show.txt",
            "Experiment  Created  train.n_estimators  train.max_depth  accuracy\n"
            "exp-maple  2026-08-15  150  12  0.9561\n"
            "exp-cedar  2026-08-15  220  8  0.9649\n",
        )
        self._write(
            "evidence/roar-show.txt",
            "@4 python3 scripts/evaluate.py commit abc123 accuracy 0.9561\n"
            "@6 python3 scripts/evaluate.py commit def456 accuracy 0.9649\n",
        )
        self._write("evidence/dvc-dag.txt", "+------------+\n| preprocess | -> train -> evaluate\n+------------+\n")
        self._write(
            "evidence/roar-dag.txt",
            "scripts/preprocess.py -> scripts/train.py -> scripts/evaluate.py\n",
        )
        self._write(
            "evidence/experiments.json",
            json.dumps(
                {
                    "dvc_experiments": [
                        {"run_id": "exp-maple", "parameters": {"n_estimators": 150, "max_depth": 12}, "results": {"accuracy": 0.9561}},
                        {"run_id": "exp-cedar", "parameters": {"n_estimators": 220, "max_depth": 8}, "results": {"accuracy": 0.9649}},
                    ],
                    "roar_experiments": [
                        {"run_id": "@4", "parameters": {"n_estimators": 150, "max_depth": 12}, "results": {"accuracy": 0.9561}},
                        {"run_id": "@6", "parameters": {"n_estimators": 220, "max_depth": 8}, "results": {"accuracy": 0.9649}},
                    ],
                },
                indent=2,
            ),
        )
        self._write(
            "evidence/lineage.json",
            json.dumps({"roar_run_id": "@6", "artifact_hash": "a" * 64}, indent=2),
        )

    def generate(self, env: dict[str, str] | None = None, screenshot: bool = False) -> subprocess.CompletedProcess[str]:
        args = [
            "python3", str(SCRIPT),
            "--learner", "Test Learner",
            "--repository-url", "https://github.com/test-learner/lab-pipeline-versioning",
            "--dvc-exp-show", "evidence/dvc-exp-show.txt",
            "--roar-show", "evidence/roar-show.txt",
            "--dvc-dag", "evidence/dvc-dag.txt",
            "--roar-dag", "evidence/roar-dag.txt",
            "--experiments", "evidence/experiments.json",
            "--lineage-reference", "evidence/lineage.json",
        ]
        if screenshot:
            args.extend(("--glaas-screenshot", "evidence/glaas-lineage.png"))
        return run(*args, cwd=self.root, env=env)

    def manifest(self) -> dict:
        return json.loads(
            (self.root / "submission" / "pipeline-versioning-manifest.json").read_text(encoding="utf-8")
        )

    def test_generates_complete_self_contained_report_without_running_tools(self) -> None:
        fake_bin = self.root / "fake-bin"
        fake_bin.mkdir()
        markers = [self.root / "dvc-was-called", self.root / "roar-was-called"]
        for command, marker in zip(("dvc", "roar"), markers):
            executable = fake_bin / command
            executable.write_text(f"#!/bin/sh\ntouch '{marker}'\nexit 99\n", encoding="utf-8")
            executable.chmod(0o755)
        env = dict(os.environ)
        env["PATH"] = f"{fake_bin}:{env['PATH']}"

        result = self.generate(env)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any(marker.exists() for marker in markers))
        manifest = self.manifest()
        report = (self.root / "submission" / "pipeline-versioning-report.html").read_text(encoding="utf-8")
        self.assertTrue(manifest["complete"])
        self.assertTrue(all(item["status"] == "present" for item in manifest["checks"]))
        self.assertEqual(manifest["identifiers"]["dvc_run_ids"], ["exp-maple", "exp-cedar"])
        self.assertEqual(manifest["identifiers"]["roar_run_ids"], ["@4", "@6"])
        self.assertEqual(manifest["manual_spot_checks"], ["reproduction_lineage", "tool_choice"])
        self.assertIn("scripts/preprocess.py -&gt; scripts/train.py", report)
        self.assertIn("exp-maple", report)

    def test_marks_dirty_revision_and_mismatched_run_identifiers_incomplete(self) -> None:
        params = self.root / "params.yaml"
        params.write_text(params.read_text(encoding="utf-8") + "# uncommitted\n", encoding="utf-8")
        self._write(
            "evidence/roar-show.txt",
            "@4 python3 scripts/evaluate.py accuracy 0.9561\n"
            "@60 python3 scripts/evaluate.py accuracy 0.9000\n",
        )

        result = self.generate()

        self.assertEqual(result.returncode, 1)
        statuses = {item["name"]: item["status"] for item in self.manifest()["checks"]}
        self.assertEqual(statuses["immutable_repository_revision"], "missing")
        self.assertEqual(statuses["roar_show_output"], "missing")
        self.assertEqual(statuses["glaas_lineage_reference"], "missing")

    def test_accepts_and_embeds_screenshot_fallback(self) -> None:
        self._write("evidence/lineage.json", json.dumps({"roar_run_id": "@6"}, indent=2))
        (self.root / "evidence/glaas-lineage.png").write_bytes(b"\x89PNG\r\n\x1a\nlineage")

        result = self.generate(screenshot=True)

        self.assertEqual(result.returncode, 0, result.stderr)
        report = (self.root / "submission" / "pipeline-versioning-report.html").read_text(encoding="utf-8")
        self.assertIn("data:image/png;base64,", report)
        self.assertTrue(self.manifest()["complete"])

    def test_rejects_malformed_experiment_summary(self) -> None:
        self._write(
            "evidence/experiments.json",
            json.dumps({"dvc_experiments": [{"run_id": "exp-maple"}], "roar_experiments": []}),
        )

        result = self.generate()

        self.assertEqual(result.returncode, 1)
        statuses = {item["name"]: item["status"] for item in self.manifest()["checks"]}
        self.assertEqual(statuses["experiment_summary_structure"], "missing")

    def test_rejects_placeholder_lineage_identifiers(self) -> None:
        self._write(
            "evidence/lineage.json",
            json.dumps(
                {
                    "roar_run_id": "@6",
                    "artifact_hash": "replace-with-the-displayed-artifact-hash",
                    "glaas_url": "https://glaas.ai/replace-with-the-stable-artifact-path",
                },
                indent=2,
            ),
        )

        result = self.generate()

        self.assertEqual(result.returncode, 1)
        statuses = {item["name"]: item["status"] for item in self.manifest()["checks"]}
        self.assertEqual(statuses["glaas_lineage_reference"], "missing")

    def test_withholds_plaintext_credentials_from_both_outputs(self) -> None:
        secret = "ghp_abcdefghijklmnopqrstuvwxyz123456"
        data = json.loads((self.root / "evidence/experiments.json").read_text(encoding="utf-8"))
        data["api_key"] = secret
        self._write("evidence/experiments.json", json.dumps(data, indent=2))

        result = self.generate()

        self.assertEqual(result.returncode, 1)
        combined = (
            (self.root / "submission/pipeline-versioning-report.html").read_text(encoding="utf-8")
            + (self.root / "submission/pipeline-versioning-manifest.json").read_text(encoding="utf-8")
        )
        self.assertNotIn(secret, combined)
        self.assertIn("Content withheld", combined)
        self.assertEqual(self.manifest()["identifiers"]["dvc_run_ids"], [])

    def test_rejects_missing_evidence_without_creating_outputs(self) -> None:
        (self.root / "evidence/roar-dag.txt").unlink()

        result = self.generate()

        self.assertEqual(result.returncode, 2)
        self.assertIn("Missing required saved evidence", result.stderr)
        self.assertFalse((self.root / "submission").exists())


if __name__ == "__main__":
    unittest.main()

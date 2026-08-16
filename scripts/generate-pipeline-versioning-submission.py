#!/usr/bin/env python3
"""Generate the Lab 9 report and manifest from durable, saved evidence only."""

from __future__ import annotations

import argparse
import base64
import html
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


REPORT_NAME = "pipeline-versioning-report.html"
MANIFEST_NAME = "pipeline-versioning-manifest.json"
CHECKER_VERSION = "1.0"
REVISION_PATHS = (
    "dvc.yaml",
    "dvc.lock",
    "params.yaml",
    "scripts/preprocess.py",
    "scripts/train.py",
    "scripts/evaluate.py",
    "scripts/augment_data.py",
)
SECRET_PATTERNS = (
    re.compile(r"(?i)(?:api[_-]?key|access[_-]?token|auth[_-]?token|password|secret|token)\s*[:=]\s*['\"]?[^\s'\"]{8,}"),
    re.compile(r"\b(?:ghp|github_pat|sk|xox[baprs])_[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"(?i)-----BEGIN (?:OPENSSH|RSA|EC|DSA) PRIVATE KEY-----"),
    re.compile(r"https?://[^\s/@:]+:[^\s/@]+@", re.IGNORECASE),
)
IMAGE_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


def run_git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=root, check=False, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True,
    )


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def check(name: str, present: bool, location: str, identifier: str, detail: str) -> dict[str, str]:
    return {
        "name": name,
        "status": "present" if present else "missing",
        "location": location,
        "identifier": identifier,
        "detail": detail,
    }


def contains_secret(text: str) -> bool:
    return any(pattern.search(text) for pattern in SECRET_PATTERNS)


def sanitize_repository_url(value: str) -> tuple[str, bool]:
    """Return a credential-free GitHub URL and whether the supplied URL was safe."""
    value = value.strip()
    scp_match = re.fullmatch(r"git@github\.com:([^/\s]+)/([^/\s]+?)(?:\.git)?", value)
    if scp_match:
        owner, repository = scp_match.groups()
        return f"https://github.com/{owner}/{repository.removesuffix('.git')}", True
    try:
        parsed = urlsplit(value)
    except ValueError:
        return "invalid-url-redacted", False
    host = parsed.hostname or ""
    parts = [part for part in parsed.path.removesuffix(".git").split("/") if part]
    valid = (
        parsed.scheme == "https" and host.casefold() == "github.com"
        and parsed.username is None and parsed.password is None
        and not parsed.query and not parsed.fragment and len(parts) == 2
    )
    if not valid:
        return "invalid-url-redacted", False
    return f"https://github.com/{parts[0]}/{parts[1]}", True


def local_display_path(root: Path, path: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return path.name


def parse_object(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(read_text(path))
    except (json.JSONDecodeError, OSError):
        return None
    return value if isinstance(value, dict) else None


def scalar_mapping(value: Any) -> bool:
    return isinstance(value, dict) and bool(value) and all(
        isinstance(key, str) and key.strip()
        and isinstance(item, (str, int, float, bool)) and not isinstance(item, type(None))
        for key, item in value.items()
    )


def parse_experiments(value: dict[str, Any] | None) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool, str]:
    if value is None:
        return [], [], False, "The experiment summary is not a JSON object."
    dvc = value.get("dvc_experiments")
    roar = value.get("roar_experiments")
    if not isinstance(dvc, list) or not isinstance(roar, list):
        return [], [], False, "Use dvc_experiments and roar_experiments arrays."

    def valid_record(record: Any) -> bool:
        return (
            isinstance(record, dict)
            and isinstance(record.get("run_id"), str)
            and bool(record["run_id"].strip())
            and scalar_mapping(record.get("parameters"))
            and scalar_mapping(record.get("results"))
        )

    dvc_ids = [str(item.get("run_id", "")).strip().casefold() for item in dvc if isinstance(item, dict)]
    roar_ids = [str(item.get("run_id", "")).strip().casefold() for item in roar if isinstance(item, dict)]
    valid = (
        len(dvc) >= 2 and len(roar) >= 2
        and len(set(dvc_ids)) == len(dvc) and len(set(roar_ids)) == len(roar)
        and all(valid_record(item) for item in dvc + roar)
    )
    detail = (
        f"Parsed {len(dvc)} DVC and {len(roar)} Roar experiments with run IDs, parameters, and results."
        if valid else
        "Record at least two experiments with unique run IDs for each tool; every record needs a non-empty run_id plus non-empty scalar parameters and results."
    )
    return dvc, roar, valid, detail


def normalized_identifier(value: Any) -> str:
    return str(value).strip().casefold()


def identifier_in_output(identifier: Any, output: str) -> bool:
    normalized = normalized_identifier(identifier)
    if not normalized:
        return False
    token_character = r"A-Za-z0-9_.@-"
    return bool(re.search(
        rf"(?<![{token_character}]){re.escape(normalized)}(?![{token_character}])",
        output.casefold(),
    ))


def identifiers_match(records: list[dict[str, Any]], output: str) -> tuple[bool, list[str]]:
    identifiers = [str(record.get("run_id", "")).strip() for record in records]
    return bool(identifiers) and all(identifier_in_output(item, output) for item in identifiers), identifiers


def valid_saved_output(text: str, required_terms: tuple[str, ...]) -> bool:
    return bool(text.strip()) and all(term.casefold() in text.casefold() for term in required_terms)


def valid_glaas_url(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = urlsplit(value.strip())
    except ValueError:
        return False
    placeholder = re.search(r"(?i)(?:replace|example|placeholder|your[-_ ]|artifact[-_ ]?path)", value)
    host = (parsed.hostname or "").casefold()
    return not placeholder and (
        parsed.scheme == "https"
        and (host == "glaas.ai" or host.endswith(".glaas.ai"))
        and parsed.username is None and parsed.password is None
        and not parsed.query and not parsed.fragment
        and bool(parsed.path.strip("/"))
    )


def valid_artifact_hash(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    compact = value.strip()
    if re.search(r"(?i)(?:replace|example|placeholder|your[-_ ]|artifact[-_ ]?hash)", compact):
        return False
    return bool(
        re.fullmatch(r"(?:sha256:)?[0-9a-fA-F]{32,128}", compact)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{15,127}", compact)
    )


def image_data_uri(path: Path | None) -> tuple[str, bool]:
    if path is None or not path.is_file() or path.stat().st_size == 0:
        return "", False
    mime_type = IMAGE_TYPES.get(path.suffix.casefold())
    if mime_type is None:
        return "", False
    content = path.read_bytes()
    signatures = {
        "image/png": content.startswith(b"\x89PNG\r\n\x1a\n"),
        "image/jpeg": content.startswith(b"\xff\xd8\xff"),
        "image/gif": content.startswith((b"GIF87a", b"GIF89a")),
        "image/webp": content.startswith(b"RIFF") and content[8:12] == b"WEBP",
    }
    if not signatures[mime_type]:
        return "", False
    encoded = base64.b64encode(content).decode("ascii")
    return f"data:{mime_type};base64,{encoded}", True


def evidence_section(title: str, location: str, content: str, withheld: bool) -> str:
    body = "Content withheld because an obvious credential pattern was detected." if withheld else content
    return (
        f'<details><summary>{html.escape(title)} — <code>{html.escape(location)}</code></summary>'
        f'<pre>{html.escape(body)}</pre></details>'
    )


def experiment_rows(records: list[dict[str, Any]], tool: str) -> str:
    rows = []
    for record in records:
        rows.append(
            "<tr>"
            f"<td>{html.escape(tool)}</td>"
            f"<td><code>{html.escape(str(record.get('run_id', '')))}</code></td>"
            f"<td><code>{html.escape(json.dumps(record.get('parameters', {}), sort_keys=True))}</code></td>"
            f"<td><code>{html.escape(json.dumps(record.get('results', {}), sort_keys=True))}</code></td>"
            "</tr>"
        )
    return "\n".join(rows)


def render_report(
    learner: str,
    generated_at: str,
    repository_url: str,
    repository_valid: bool,
    commit_sha: str,
    checks: list[dict[str, str]],
    evidence: list[tuple[str, str, str]],
    withheld: set[str],
    dvc_experiments: list[dict[str, Any]],
    roar_experiments: list[dict[str, Any]],
    lineage: dict[str, Any] | None,
    screenshot_uri: str,
) -> str:
    overall = "complete" if all(item["status"] == "present" for item in checks) else "incomplete"
    rows = "\n".join(
        "<tr>"
        f"<td>{html.escape(item['name'])}</td>"
        f"<td class=\"{item['status']}\">{item['status']}</td>"
        f"<td>{html.escape(item['identifier'])}</td>"
        f"<td>{html.escape(item['detail'])}</td>"
        "</tr>"
        for item in checks
    )
    commit_url = f"{repository_url}/commit/{commit_sha}" if repository_valid else repository_url
    evidence_sections = "\n".join(
        evidence_section(title, location, content, location in withheld)
        for title, location, content in evidence
    )
    experiments = experiment_rows(dvc_experiments, "DVC") + experiment_rows(roar_experiments, "Roar")
    lineage_text = json.dumps(lineage, indent=2, sort_keys=True) if lineage is not None else "Lineage reference could not be parsed."
    lineage_location = next((location for title, location, _content in evidence if title == "Lineage reference"), "lineage reference")
    if lineage_location in withheld:
        lineage_text = "Content withheld because an obvious credential pattern was detected."
    screenshot = f'<img src="{screenshot_uri}" alt="Saved GLaaS lineage screenshot">' if screenshot_uri else "<p>No screenshot was supplied.</p>"
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Lab 9 Pipeline Versioning Submission Report</title>
  <style>
    body {{ color:#17202a; font:16px/1.45 system-ui,sans-serif; margin:2rem auto; max-width:76rem; padding:0 1rem; }}
    h1,h2 {{ color:#102a43; }} table {{ border-collapse:collapse; width:100%; }}
    th,td {{ border:1px solid #bcccdc; padding:.55rem; text-align:left; vertical-align:top; }} th {{ background:#f0f4f8; }}
    pre {{ background:#f5f7fa; border:1px solid #bcccdc; overflow:auto; padding:1rem; white-space:pre-wrap; }}
    code {{ overflow-wrap:anywhere; }} details {{ margin:.7rem 0; }} summary {{ cursor:pointer; font-weight:650; }}
    img {{ border:1px solid #bcccdc; height:auto; max-width:100%; }}
    .present,.complete {{ color:#176b3a; font-weight:700; }} .missing,.incomplete {{ color:#a61b1b; font-weight:700; }}
    .note {{ background:#fffbea; border-left:.3rem solid #d69e2e; padding:.7rem 1rem; }}
  </style>
</head>
<body>
  <h1>Lab 9: Data &amp; Pipeline Versioning Submission Report</h1>
  <table>
    <tr><th>Learner</th><td>{html.escape(learner)}</td></tr>
    <tr><th>Generated</th><td>{html.escape(generated_at)}</td></tr>
    <tr><th>Overall completeness</th><td class="{overall}">{overall}</td></tr>
    <tr><th>Immutable repository revision</th><td><a href="{html.escape(commit_url)}">{html.escape(commit_url)}</a></td></tr>
    <tr><th>Commit SHA</th><td><code>{html.escape(commit_sha)}</code></td></tr>
    <tr><th>Checker version</th><td>{CHECKER_VERSION}</td></tr>
  </table>

  <h2>Completeness</h2>
  <table><thead><tr><th>Required evidence</th><th>Status</th><th>Identifier</th><th>Detail</th></tr></thead><tbody>{rows}</tbody></table>

  <h2>Experiment parameters and results</h2>
  <table><thead><tr><th>Tool</th><th>Run ID</th><th>Parameters</th><th>Results</th></tr></thead><tbody>{experiments}</tbody></table>

  <h2>GLaaS lineage evidence</h2>
  <pre>{html.escape(lineage_text)}</pre>
  {screenshot}

  <h2>Complete saved command outputs</h2>
  {evidence_sections}

  <h2>Staff spot checks</h2>
  <p class="note">This checker confirms objective completeness and matching run identifiers only. Answer these separately in Canvas; it does not judge either answer.</p>
  <ol>
    <li><strong>Reproduction and lineage:</strong> explain one DVC rerun and trace one saved Roar or GLaaS artifact to its producing run and data.</li>
    <li><strong>Tool choice:</strong> compare one DAG relationship and one meaningful difference, then recommend an approach for the team project.</li>
  </ol>

  <h2>Safety and durability</h2>
  <p>The report embeds the saved text and screenshot, so it remains reviewable without the DVC remote or GLaaS. The checker scans included text for common plaintext credential patterns. Review images and the finished report yourself before uploading.</p>
</body>
</html>
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate the Lab 9 HTML report and JSON manifest without running DVC, Roar, or contacting GLaaS."
    )
    parser.add_argument("--learner", required=True)
    parser.add_argument("--repository-url", required=True, help="HTTPS or git@github.com repository URL")
    parser.add_argument("--dvc-exp-show", required=True, type=Path)
    parser.add_argument("--roar-show", required=True, type=Path)
    parser.add_argument("--dvc-dag", required=True, type=Path)
    parser.add_argument("--roar-dag", required=True, type=Path)
    parser.add_argument("--experiments", required=True, type=Path, help="JSON experiment summary documented in README.md")
    parser.add_argument("--lineage-reference", required=True, type=Path, help="JSON with roar_run_id and a GLaaS URL or artifact hash")
    parser.add_argument("--glaas-screenshot", type=Path, help="PNG, JPEG, WebP, or GIF fallback/companion evidence")
    parser.add_argument("--output-dir", default=Path("submission"), type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root_result = run_git(Path.cwd(), "rev-parse", "--show-toplevel")
    if root_result.returncode != 0:
        print("Run this command from inside the lab-pipeline-versioning repository.", file=sys.stderr)
        return 2
    root = Path(root_result.stdout.strip())

    inputs = {
        "DVC experiment output": args.dvc_exp_show,
        "Roar show output": args.roar_show,
        "DVC DAG output": args.dvc_dag,
        "Roar DAG output": args.roar_dag,
        "Experiment summary": args.experiments,
        "Lineage reference": args.lineage_reference,
    }
    if args.glaas_screenshot is not None:
        inputs["GLaaS lineage screenshot"] = args.glaas_screenshot
    missing = [f"{label}: {path}" for label, path in inputs.items() if not path.is_file()]
    if missing:
        print("Missing required saved evidence:\n  " + "\n  ".join(missing), file=sys.stderr)
        return 2

    head_result = run_git(root, "rev-parse", "HEAD")
    if head_result.returncode != 0:
        print("The repository has no commit to use as an immutable revision.", file=sys.stderr)
        return 2
    commit_sha = head_result.stdout.strip()
    repository_url, repository_valid = sanitize_repository_url(args.repository_url)
    origin_result = run_git(root, "remote", "get-url", "origin")
    origin_url, origin_valid = sanitize_repository_url(origin_result.stdout) if origin_result.returncode == 0 else ("origin-not-found", False)
    repository_matches_origin = repository_valid and origin_valid and repository_url.casefold() == origin_url.casefold()
    tracked = set(run_git(root, "ls-tree", "-r", "--name-only", "HEAD").stdout.splitlines())
    diff_result = run_git(root, "diff", "--quiet", "HEAD", "--", *REVISION_PATHS)
    immutable = all(path in tracked for path in REVISION_PATHS) and diff_result.returncode == 0 and repository_matches_origin

    displayed = {label: local_display_path(root, path) for label, path in inputs.items()}
    text_inputs = {label: read_text(path) for label, path in inputs.items() if label != "GLaaS lineage screenshot"}
    dvc_output = text_inputs["DVC experiment output"]
    roar_output = text_inputs["Roar show output"]
    dvc_dag = text_inputs["DVC DAG output"]
    roar_dag = text_inputs["Roar DAG output"]
    experiment_data = parse_object(args.experiments)
    dvc_experiments, roar_experiments, experiments_ok, experiments_detail = parse_experiments(experiment_data)
    dvc_ids_match, dvc_ids = identifiers_match(dvc_experiments, dvc_output)
    roar_ids_match, roar_ids = identifiers_match(roar_experiments, roar_output)

    dvc_output_ok = valid_saved_output(dvc_output, ("experiment",)) and dvc_ids_match
    roar_output_ok = bool(roar_output.strip()) and roar_ids_match
    dvc_dag_ok = valid_saved_output(dvc_dag, ("preprocess", "train", "evaluate"))
    roar_dag_ok = valid_saved_output(roar_dag, ("preprocess", "train", "evaluate"))

    lineage = parse_object(args.lineage_reference)
    lineage_run_id = str(lineage.get("roar_run_id", "")).strip() if lineage else ""
    lineage_matches = identifier_in_output(lineage_run_id, roar_output)
    artifact_hash = str(lineage.get("artifact_hash", "")).strip() if lineage else ""
    glaas_url = str(lineage.get("glaas_url", "")).strip() if lineage else ""
    screenshot_uri, screenshot_ok = image_data_uri(args.glaas_screenshot)
    reference_ok = valid_artifact_hash(artifact_hash) or valid_glaas_url(glaas_url)
    lineage_ok = bool(lineage and lineage_matches and (reference_ok or screenshot_ok))

    withheld = {
        displayed[label] for label, content in text_inputs.items() if contains_secret(content)
    }
    secrets_ok = not withheld
    experiment_withheld = displayed["Experiment summary"] in withheld
    lineage_withheld = displayed["Lineage reference"] in withheld
    safe_dvc_ids = [] if experiment_withheld else dvc_ids
    safe_roar_ids = [] if experiment_withheld else roar_ids
    safe_lineage_identifier = "content withheld" if lineage_withheld else (
        artifact_hash or glaas_url or ("embedded screenshot" if screenshot_ok else "reference missing")
    )
    report_location = REPORT_NAME
    checks = [
        check(
            "immutable_repository_revision", immutable, report_location, commit_sha,
            "The pipeline definition, lock file, parameters, and scripts are tracked and unchanged at HEAD, and the credential-free repository URL matches origin."
            if immutable else
            "Commit dvc.yaml, dvc.lock, params.yaml, and all pipeline scripts unchanged at HEAD, then provide the credential-free GitHub URL configured as origin.",
        ),
        check(
            "experiment_summary_structure", experiments_ok, displayed["Experiment summary"],
            f"dvc:{len(dvc_experiments)}; roar:{len(roar_experiments)}", experiments_detail,
        ),
        check(
            "dvc_exp_show_output", dvc_output_ok, displayed["DVC experiment output"],
            ", ".join(safe_dvc_ids) or ("content withheld" if experiment_withheld else "no run IDs parsed"),
            "Every reported DVC run ID appears in the saved dvc exp show output."
            if dvc_output_ok else "Save complete dvc exp show output containing every DVC run ID from the experiment summary.",
        ),
        check(
            "roar_show_output", roar_output_ok, displayed["Roar show output"],
            ", ".join(safe_roar_ids) or ("content withheld" if experiment_withheld else "no run IDs parsed"),
            "Every reported Roar run ID appears in the saved roar show output."
            if roar_output_ok else "Save complete roar show output containing every Roar run ID from the experiment summary.",
        ),
        check(
            "dvc_dag_output", dvc_dag_ok, displayed["DVC DAG output"], "preprocess, train, evaluate" if dvc_dag_ok else "stage names missing",
            "The saved DVC DAG names all three pipeline stages." if dvc_dag_ok else "Save dvc dag output that names preprocess, train, and evaluate.",
        ),
        check(
            "roar_dag_output", roar_dag_ok, displayed["Roar DAG output"], "preprocess, train, evaluate" if roar_dag_ok else "stage names missing",
            "The saved Roar DAG identifies all three observed pipeline stages." if roar_dag_ok else "Save roar dag output that identifies preprocess, train, and evaluate.",
        ),
        check(
            "glaas_lineage_reference", lineage_ok, displayed["Lineage reference"],
            safe_lineage_identifier,
            "The saved GLaaS hash/link or screenshot is tied to a Roar run ID found in roar show."
            if lineage_ok else "Provide roar_run_id plus a GLaaS artifact_hash or glaas_url; an image screenshot may replace the hash/link. The run ID must appear in roar show.",
        ),
        check(
            "durable_embedded_evidence", all(bool(value.strip()) for value in text_inputs.values()) and (args.glaas_screenshot is None or screenshot_ok),
            report_location, "saved text embedded" + ("; screenshot embedded" if screenshot_ok else ""),
            "All saved text evidence is non-empty and embedded in the self-contained report."
            if all(bool(value.strip()) for value in text_inputs.values()) and (args.glaas_screenshot is None or screenshot_ok)
            else "Use non-empty text files and, if supplied, a non-empty PNG, JPEG, WebP, or GIF screenshot.",
        ),
        check(
            "obvious_credential_leakage", secrets_ok, report_location,
            "none detected" if secrets_ok else "content withheld",
            "No common plaintext credential pattern was found."
            if secrets_ok else "Remove and revoke plaintext credentials before regenerating; affected content was withheld.",
        ),
    ]

    evidence = [
        (label, displayed[label], text_inputs[label])
        for label in ("DVC experiment output", "Roar show output", "DVC DAG output", "Roar DAG output", "Experiment summary", "Lineage reference")
    ]
    generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    complete = all(item["status"] == "present" for item in checks)
    report = render_report(
        args.learner, generated_at, repository_url, repository_valid, commit_sha,
        checks, evidence, withheld,
        [] if experiment_withheld else dvc_experiments,
        [] if experiment_withheld else roar_experiments,
        None if lineage_withheld else lineage,
        screenshot_uri,
    )
    manifest = {
        "schema_version": "1.0",
        "lab": "Lab 9: Data & Pipeline Versioning — DVC and Roar",
        "learner": args.learner,
        "generated_at": generated_at,
        "checker_version": CHECKER_VERSION,
        "complete": complete,
        "report": REPORT_NAME,
        "identifiers": {
            "repository_url": repository_url,
            "commit_sha": commit_sha,
            "dvc_run_ids": safe_dvc_ids,
            "roar_run_ids": safe_roar_ids,
            "lineage_roar_run_id": None if lineage_withheld else (lineage_run_id or None),
            "glaas_artifact_hash": None if lineage_withheld else (artifact_hash or None),
            "glaas_url": None if lineage_withheld else (glaas_url if valid_glaas_url(glaas_url) else None),
        },
        "checks": checks,
        "manual_spot_checks": ["reproduction_lineage", "tool_choice"],
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / REPORT_NAME).write_text(report, encoding="utf-8")
    (args.output_dir / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {args.output_dir / REPORT_NAME}")
    print(f"Wrote {args.output_dir / MANIFEST_NAME}")
    print("Submission evidence is complete." if complete else "Submission evidence is incomplete; open the report for details.")
    return 0 if complete else 1


if __name__ == "__main__":
    raise SystemExit(main())

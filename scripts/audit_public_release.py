#!/usr/bin/env python3
"""Audit the repository material that would become public.

The default mode fails closed on both technical findings and unreviewed
publication decisions. ``--technical-only`` is intended for ordinary CI while
the repository is still being prepared; tagged releases must use the default.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import re
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
POLICY_NAME = "PUBLICATION.toml"

FORBIDDEN_COMPONENTS = {
    ".coverage",
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".superdesign",
    ".venv",
    "__pycache__",
    "artifacts",
    "build",
    "dist",
    "graphify-out",
    "htmlcov",
    "source",
}
FORBIDDEN_SUFFIXES = {
    ".7z",
    ".ckpt",
    ".db",
    ".dmg",
    ".env",
    ".onnx",
    ".pdf",
    ".pem",
    ".pfx",
    ".pkl",
    ".safetensors",
    ".sqlite",
    ".sqlite3",
}
TEXT_SUFFIXES = {
    "",
    ".css",
    ".gitignore",
    ".html",
    ".ini",
    ".json",
    ".lock",
    ".md",
    ".py",
    ".rst",
    ".toml",
    ".tsv",
    ".txt",
    ".yaml",
    ".yml",
}
ABSOLUTE_PATH_PATTERNS = (
    re.compile(rb"/Users/[A-Za-z0-9._-]+/"),
    re.compile(rb"/home/[A-Za-z0-9._-]+/"),
    re.compile(rb"[A-Za-z]:\\\\Users\\\\[^\\\r\n]+", re.IGNORECASE),
)
SECRET_PATTERNS = {
    "private key": re.compile(rb"BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY"),
    "AWS access key": re.compile(rb"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "GitHub token": re.compile(rb"\bgh[oprsu]_[A-Za-z0-9]{36,255}\b"),
    "Google API key": re.compile(rb"\bAIza[0-9A-Za-z_-]{35}\b"),
    "Slack token": re.compile(rb"\bxox[baprs]-[0-9A-Za-z-]{10,}\b"),
    "Stripe live key": re.compile(rb"\b(?:sk|rk)_live_[0-9A-Za-z]{16,}\b"),
}


@dataclass(frozen=True, order=True)
class Finding:
    category: str
    location: str
    message: str


def _git(root: Path, *args: str, input_text: str | None = None) -> str:
    process = subprocess.run(
        ["git", *args],
        cwd=root,
        input=input_text,
        text=True,
        capture_output=True,
        check=False,
    )
    if process.returncode:
        detail = process.stderr.strip() or process.stdout.strip()
        raise RuntimeError(f"git {' '.join(args)} failed: {detail}")
    return process.stdout


def publishable_paths(root: Path) -> list[str]:
    output = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=root,
        capture_output=True,
        check=True,
    ).stdout
    paths = []
    for raw in output.split(b"\0"):
        if not raw:
            continue
        path = raw.decode("utf-8", errors="strict")
        if (root / path).exists() or (root / path).is_symlink():
            paths.append(path)
    return sorted(set(paths))


def history_paths_and_sizes(root: Path) -> dict[str, int]:
    objects = _git(root, "rev-list", "--objects", "--all")
    oid_paths: dict[str, str] = {}
    for line in objects.splitlines():
        oid, separator, path = line.partition(" ")
        if separator:
            oid_paths.setdefault(oid, path)
    if not oid_paths:
        return {}

    batch = _git(
        root,
        "cat-file",
        "--batch-check=%(objectname) %(objecttype) %(objectsize)",
        input_text="\n".join(oid_paths) + "\n",
    )
    paths: dict[str, int] = {}
    for line in batch.splitlines():
        oid, object_type, size = line.split()
        if object_type == "blob":
            paths[oid_paths[oid]] = max(paths.get(oid_paths[oid], 0), int(size))
    return paths


def _path_findings(path: str, *, scope: str) -> list[Finding]:
    pure = PurePosixPath(path)
    findings: list[Finding] = []
    if pure.is_absolute() or not pure.parts or ".." in pure.parts:
        findings.append(Finding("technical", path, f"unsafe {scope} path"))
        return findings
    if any(
        part in FORBIDDEN_COMPONENTS or part.startswith(".venv-") for part in pure.parts
    ):
        findings.append(Finding("technical", path, f"forbidden {scope} path"))
    name_lower = pure.name.lower()
    if (
        name_lower == ".env" or name_lower.startswith(".env.")
    ) and name_lower != ".env.example":
        findings.append(Finding("technical", path, f"environment file in {scope}"))
    if pure.suffix.lower() in FORBIDDEN_SUFFIXES:
        findings.append(Finding("technical", path, f"forbidden file type in {scope}"))
    if any(ord(character) < 32 for character in path):
        findings.append(
            Finding("technical", path, f"control character in {scope} path")
        )
    return findings


def _content_findings(root: Path, path: str, limit: int) -> list[Finding]:
    full_path = root / path
    findings: list[Finding] = []
    if full_path.is_symlink():
        return [Finding("technical", path, "symlinks are not publishable")]
    if not full_path.is_file():
        return [Finding("technical", path, "publishable path is not a regular file")]
    size = full_path.stat().st_size
    if size > limit:
        findings.append(
            Finding("technical", path, f"file is {size} bytes; limit is {limit}")
        )

    data = full_path.read_bytes()
    suffix = full_path.suffix.lower()
    if suffix == ".png" and not data.startswith(b"\x89PNG\r\n\x1a\n"):
        findings.append(
            Finding("technical", path, "extension is .png but content is not PNG")
        )
    if suffix in {".jpg", ".jpeg"} and not data.startswith(b"\xff\xd8\xff"):
        findings.append(
            Finding("technical", path, "extension is JPEG but content is not JPEG")
        )

    if suffix in TEXT_SUFFIXES:
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            findings.append(
                Finding("technical", path, "declared text file is not UTF-8")
            )
            return findings
        for pattern in ABSOLUTE_PATH_PATTERNS:
            if pattern.search(data):
                findings.append(
                    Finding(
                        "technical", path, "contains a machine-specific absolute path"
                    )
                )
                break
        for label, pattern in SECRET_PATTERNS.items():
            if pattern.search(data):
                findings.append(
                    Finding("technical", path, f"contains a possible {label}")
                )
    return findings


def _asset_review_findings(
    policy: dict[str, object], all_paths: set[str]
) -> list[Finding]:
    findings: list[Finding] = []
    groups = policy.get("asset_review")
    if not isinstance(groups, list) or not groups:
        return [Finding("technical", POLICY_NAME, "asset_review records are required")]
    valid_decisions = {"approved", "excluded", "pending"}
    for record in groups:
        if not isinstance(record, dict):
            findings.append(
                Finding("technical", POLICY_NAME, "invalid asset_review record")
            )
            continue
        name = str(record.get("name", "")).strip()
        globs = record.get("globs")
        decision = str(record.get("decision", "")).strip()
        if not name or not isinstance(globs, list) or not globs:
            findings.append(
                Finding("technical", POLICY_NAME, "asset_review needs name and globs")
            )
            continue
        if decision not in valid_decisions:
            findings.append(
                Finding("technical", name, f"invalid review decision {decision!r}")
            )
            continue
        matched = {
            path
            for path in all_paths
            if any(fnmatch.fnmatchcase(path, str(pattern)) for pattern in globs)
        }
        if not matched:
            findings.append(
                Finding(
                    "technical", name, "asset review does not match the tree or history"
                )
            )
            continue
        if decision == "pending":
            findings.append(
                Finding(
                    "governance",
                    name,
                    f"rights review pending for {len(matched)} path(s)",
                )
            )
        elif decision == "excluded":
            findings.append(
                Finding(
                    "governance",
                    name,
                    f"excluded material is still reachable at {len(matched)} path(s)",
                )
            )
        else:
            license_expression = str(record.get("license_expression", "")).strip()
            rights_basis = str(record.get("rights_basis", "")).strip()
            if not license_expression or not rights_basis:
                findings.append(
                    Finding(
                        "governance",
                        name,
                        "approved material lacks license and rights basis",
                    )
                )
    return findings


def audit(
    root: Path, *, technical_only: bool = False
) -> tuple[list[Finding], dict[str, int]]:
    policy_path = root / POLICY_NAME
    if not policy_path.is_file():
        return [Finding("technical", POLICY_NAME, "publication policy is missing")], {}
    policy = tomllib.loads(policy_path.read_text(encoding="utf-8"))
    if policy.get("schema_version") != 1:
        return [Finding("technical", POLICY_NAME, "unsupported schema_version")], {}

    worktree_limit = int(policy.get("max_worktree_file_bytes", 0))
    history_limit = int(policy.get("max_history_blob_bytes", 0))
    if worktree_limit <= 0 or history_limit <= 0:
        return [
            Finding("technical", POLICY_NAME, "positive file-size limits are required")
        ], {}

    current_paths = publishable_paths(root)
    history = history_paths_and_sizes(root)
    findings: list[Finding] = []
    for path in current_paths:
        findings.extend(_path_findings(path, scope="worktree"))
        findings.extend(_content_findings(root, path, worktree_limit))
    for path, size in history.items():
        findings.extend(_path_findings(path, scope="history"))
        if size > history_limit:
            findings.append(
                Finding(
                    "technical",
                    path,
                    f"historical blob is {size} bytes; limit is {history_limit}",
                )
            )

    findings.extend(_asset_review_findings(policy, set(current_paths) | set(history)))

    reviewed = policy.get("reviewed_author_email_sha256")
    if not isinstance(reviewed, list) or any(
        not isinstance(item, str) for item in reviewed
    ):
        findings.append(
            Finding(
                "technical",
                POLICY_NAME,
                "reviewed email digests must be a list of strings",
            )
        )
        reviewed_set: set[str] = set()
    else:
        reviewed_set = set(reviewed)
    emails = {
        line.strip().lower()
        for line in _git(root, "log", "--all", "--format=%ae").splitlines()
        if line.strip()
    }
    for email in emails:
        digest = hashlib.sha256(email.encode()).hexdigest()
        if digest not in reviewed_set:
            findings.append(
                Finding(
                    "governance",
                    "Git history",
                    f"unreviewed author email digest: {digest}",
                )
            )

    summary = {
        "worktree_files": len(current_paths),
        "history_paths": len(history),
        "author_emails": len(emails),
    }
    if technical_only:
        findings = [finding for finding in findings if finding.category == "technical"]
    return sorted(set(findings)), summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument(
        "--technical-only",
        action="store_true",
        help="do not fail on pending human publication decisions",
    )
    args = parser.parse_args()

    findings, summary = audit(args.root.resolve(), technical_only=args.technical_only)
    mode = "technical" if args.technical_only else "release"
    print(
        f"public audit ({mode}): {summary.get('worktree_files', 0)} worktree files, "
        f"{summary.get('history_paths', 0)} historical paths, "
        f"{summary.get('author_emails', 0)} author email(s)"
    )
    if findings:
        for finding in findings:
            print(f"FAIL [{finding.category}] {finding.location}: {finding.message}")
        return 1
    print("public audit: passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

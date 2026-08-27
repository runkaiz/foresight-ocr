#!/usr/bin/env python3
"""Build, smoke-test, and archive the standalone CLI for the current platform."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import tomllib
from importlib.metadata import Distribution, distribution, distributions
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parents[1]


def _installed_distribution_names() -> set[str]:
    return {
        canonicalize_name(item.metadata["Name"])
        for item in distributions()
        if item.metadata["Name"]
    }


def _verify_build_environment(project: dict[str, object]) -> None:
    """Keep development and audit tools out of redistributable executables."""
    optional = project.get("optional-dependencies", {})
    assert isinstance(optional, dict)
    forbidden: set[str] = set()
    for extra in ("dev", "audit"):
        requirements = optional.get(extra, [])
        assert isinstance(requirements, list)
        forbidden.update(
            canonicalize_name(Requirement(str(raw)).name) for raw in requirements
        )
    installed = forbidden & _installed_distribution_names()
    if installed:
        raise SystemExit(
            "standalone builds require a release-only environment; remove "
            f"development/audit distributions: {', '.join(sorted(installed))}"
        )


def _runtime_distributions(root: str = "foresight-ocr") -> list[Distribution]:
    """Resolve the installed runtime dependency closure for legal notices."""
    pending = [root]
    seen: set[str] = set()
    resolved: list[Distribution] = []
    while pending:
        name = canonicalize_name(pending.pop())
        if name in seen:
            continue
        seen.add(name)
        current = distribution(name)
        if name != canonicalize_name(root):
            resolved.append(current)
        for raw_requirement in current.requires or []:
            requirement = Requirement(raw_requirement)
            if requirement.marker is None or requirement.marker.evaluate({"extra": ""}):
                pending.append(requirement.name)
    return sorted(
        resolved,
        key=lambda item: canonicalize_name(item.metadata["Name"]),
    )


def _license_files(current: Distribution) -> list[tuple[str, str]]:
    """Read and deduplicate license/notice files shipped by one wheel."""
    candidates = []
    for item in current.files or []:
        basename = Path(str(item)).name.lower()
        if basename.startswith(("license", "copying", "notice")):
            candidates.append(item)

    found: list[tuple[str, str]] = []
    seen_content: set[str] = set()
    for item in sorted(candidates, key=str):
        path = Path(str(current.locate_file(item)))
        if not path.is_file():
            continue
        content = path.read_text(encoding="utf-8", errors="replace").strip()
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if not content or digest in seen_content:
            continue
        seen_content.add(digest)
        found.append((str(item), content))
    return found


def _write_third_party_notices(path: Path) -> None:
    """Bundle all runtime dependency notices required for redistribution."""
    sections = [
        "Foresight OCR bundled third-party notices",
        "",
        "This standalone archive includes the following runtime dependencies. ",
        "Their license and notice files are reproduced verbatim below.",
    ]
    for current in _runtime_distributions():
        name = current.metadata["Name"]
        licenses = _license_files(current)
        if not licenses:
            raise SystemExit(
                f"installed dependency {name} {current.version} ships no "
                "license, copying, or notice file"
            )
        expression = current.metadata.get("License-Expression")
        legacy = (current.metadata.get("License") or "").strip()
        declared = expression or (
            legacy if legacy and "\n" not in legacy else "see license files below"
        )
        sections.extend(
            [
                "",
                "=" * 79,
                f"{name} {current.version}",
                f"Declared license: {declared.strip()}",
            ]
        )
        for filename, content in licenses:
            sections.extend(
                [
                    "",
                    f"--- {filename} ---",
                    content,
                ]
            )
    path.write_text("\n".join(sections) + "\n", encoding="utf-8")


def _target() -> str:
    systems = {"Darwin": "macos", "Linux": "linux", "Windows": "windows"}
    machines = {
        "AMD64": "x86_64",
        "arm64": "arm64",
        "aarch64": "arm64",
        "x86_64": "x86_64",
    }
    try:
        system = systems[platform.system()]
        machine = machines[platform.machine()]
    except KeyError as exc:
        raise SystemExit(
            f"unsupported standalone target: {platform.platform()}"
        ) from exc
    return f"{system}-{machine}"


def _run(command: list[str], *, cwd: Path = ROOT) -> None:
    subprocess.run(command, cwd=cwd, check=True)


def _requires_signing(version: str) -> bool:
    """Public 1.x binaries are never allowed to fall back to ad-hoc signatures."""
    major = int(version.split(".", 1)[0])
    return major >= 1 or os.environ.get("FORESIGHT_REQUIRE_SIGNING") == "1"


def _macos_code_files(root: Path) -> list[Path]:
    """Return Mach-O files inside-out so nested code is signed before launchers."""
    code: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        result = subprocess.run(
            ["/usr/bin/file", "-b", str(path)],
            check=True,
            capture_output=True,
            text=True,
        )
        if "Mach-O" in result.stdout:
            code.append(path)
    return sorted(code, key=lambda path: (len(path.parts), str(path)), reverse=True)


def _macos_frameworks(root: Path) -> list[Path]:
    """Return real framework bundles inside-out, excluding symlink aliases."""
    return sorted(
        (
            path
            for path in root.rglob("*.framework")
            if path.is_dir() and not path.is_symlink()
        ),
        key=lambda path: (len(path.parts), str(path)),
        reverse=True,
    )


def _version_tuple(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in value.split("."))


def _verify_macos_floor(bundle: Path, maximum: str = "14.0") -> None:
    """Refuse code that raises the documented macOS deployment floor."""
    found: list[tuple[tuple[int, ...], Path]] = []
    for path in _macos_code_files(bundle):
        result = subprocess.run(
            ["/usr/bin/otool", "-l", str(path)],
            check=True,
            capture_output=True,
            text=True,
        )
        versions = re.findall(
            r"^\s+minos ([0-9]+(?:\.[0-9]+)+)", result.stdout, re.MULTILINE
        )
        found.extend((_version_tuple(version), path) for version in versions)
    if not found:
        raise SystemExit(f"no macOS deployment targets found in {bundle}")
    required, path = max(found)
    if required > _version_tuple(maximum):
        rendered = ".".join(map(str, required))
        raise SystemExit(
            f"{path} requires macOS {rendered}; documented maximum floor is {maximum}"
        )


def _verify_linux_glibc_floor(bundle: Path, maximum: str = "2.35") -> None:
    """Keep Linux archives runnable on the documented Ubuntu 22.04 baseline."""
    readelf = shutil.which("readelf")
    file_tool = shutil.which("file")
    if not readelf or not file_tool:
        raise SystemExit("Linux binary verification requires file and readelf")
    found: list[tuple[tuple[int, ...], Path]] = []
    for path in bundle.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        kind = subprocess.run(
            [file_tool, "-b", str(path)],
            check=True,
            capture_output=True,
            text=True,
        )
        if "ELF" not in kind.stdout:
            continue
        result = subprocess.run(
            [readelf, "--version-info", str(path)],
            check=True,
            capture_output=True,
            text=True,
        )
        versions = re.findall(r"\bGLIBC_([0-9]+\.[0-9]+)\b", result.stdout)
        found.extend((_version_tuple(version), path) for version in versions)
    if not found:
        raise SystemExit(f"no GLIBC requirements found in {bundle}")
    required, path = max(found)
    if required > _version_tuple(maximum):
        rendered = ".".join(map(str, required))
        raise SystemExit(
            f"{path} requires GLIBC {rendered}; documented maximum floor is {maximum}"
        )


def _run_codesign(command: list[str], *, attempts: int = 4) -> None:
    """Retry only Apple's transient timestamp-service failure."""
    for attempt in range(1, attempts + 1):
        result = subprocess.run(
            command,
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            if result.stdout:
                sys.stdout.write(result.stdout)
            if result.stderr:
                sys.stderr.write(result.stderr)
            return
        output = f"{result.stdout}\n{result.stderr}"
        transient = "timestamp service is not available" in output.casefold()
        if not transient or attempt == attempts:
            if result.stdout:
                sys.stdout.write(result.stdout)
            if result.stderr:
                sys.stderr.write(result.stderr)
            result.check_returncode()
        delay = 2 ** (attempt - 1)
        print(
            "Apple timestamp service was unavailable; retrying codesign "
            f"in {delay}s ({attempt}/{attempts}).",
            file=sys.stderr,
        )
        time.sleep(delay)


def _codesign_macos_path(path: Path, identity: str) -> None:
    _run_codesign(
        [
            "/usr/bin/codesign",
            "--force",
            "--options",
            "runtime",
            "--timestamp",
            "--sign",
            identity,
            str(path),
        ]
    )


def _sign_macos(bundle: Path, identity: str) -> None:
    code = _macos_code_files(bundle)
    if not code:
        raise SystemExit(f"no Mach-O files found in {bundle}")
    for path in code:
        _codesign_macos_path(path, identity)
        _run(["/usr/bin/codesign", "--verify", "--strict", str(path)])
    for framework in _macos_frameworks(bundle):
        _codesign_macos_path(framework, identity)
        _run(
            [
                "/usr/bin/codesign",
                "--verify",
                "--deep",
                "--strict",
                str(framework),
            ]
        )


def _copy_release_stage(
    source: Path, stage: Path, *, preserve_symlinks: bool = False
) -> None:
    """Copy a bundle, preserving links only when its platform requires them."""
    if stage.exists():
        shutil.rmtree(stage)
    shutil.copytree(source, stage, symlinks=preserve_symlinks)


def _sign_windows(
    bundle: Path, certificate: Path, password: str, timestamp: str
) -> None:
    signtool = os.environ.get("FORESIGHT_SIGNTOOL")
    if not signtool or not Path(signtool).is_file():
        raise SystemExit("FORESIGHT_SIGNTOOL must name the Windows SDK signtool.exe")
    code = sorted(
        path
        for path in bundle.rglob("*")
        if path.is_file() and path.suffix.lower() in {".exe", ".dll", ".pyd"}
    )
    if not code:
        raise SystemExit(f"no Authenticode-signable files found in {bundle}")
    for path in code:
        _run(
            [
                signtool,
                "sign",
                "/fd",
                "SHA256",
                "/tr",
                timestamp,
                "/td",
                "SHA256",
                "/f",
                str(certificate),
                "/p",
                password,
                str(path),
            ]
        )
        _run([signtool, "verify", "/pa", "/v", str(path)])


def _verify_macos_notarization(executable: Path) -> None:
    """Verify notarization for non-app code using Apple's requirement check."""
    _run(
        [
            "/usr/bin/codesign",
            "--verify",
            "--strict",
            "--verbose=4",
            "-R=notarized",
            "--check-notarization",
            str(executable),
        ]
    )


def _notarize_macos(stage: Path, executable: Path) -> None:
    apple_id = os.environ.get("FORESIGHT_APPLE_ID")
    team_id = os.environ.get("FORESIGHT_APPLE_TEAM_ID")
    password = os.environ.get("FORESIGHT_APPLE_APP_PASSWORD")
    if not all((apple_id, team_id, password)):
        raise SystemExit(
            "macOS 1.0 notarization requires FORESIGHT_APPLE_ID, "
            "FORESIGHT_APPLE_TEAM_ID, and FORESIGHT_APPLE_APP_PASSWORD"
        )
    assert apple_id is not None and team_id is not None and password is not None
    credentials = [
        "--apple-id",
        apple_id,
        "--team-id",
        team_id,
        "--password",
        password,
    ]
    with tempfile.TemporaryDirectory(prefix="foresight-ocr-notary-") as temp:
        upload = Path(temp) / f"{stage.name}.zip"
        _run(["/usr/bin/ditto", "-c", "-k", "--keepParent", str(stage), str(upload)])
        result = subprocess.run(
            [
                "/usr/bin/xcrun",
                "notarytool",
                "submit",
                str(upload),
                *credentials,
                "--wait",
                "--output-format",
                "json",
            ],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        response = json.loads(result.stdout)
        if response.get("status") != "Accepted":
            issue_summary = ""
            submission_id = response.get("id")
            if submission_id:
                log_result = subprocess.run(
                    [
                        "/usr/bin/xcrun",
                        "notarytool",
                        "log",
                        str(submission_id),
                        *credentials,
                        "--output-format",
                        "json",
                    ],
                    cwd=ROOT,
                    check=False,
                    capture_output=True,
                    text=True,
                )
                if log_result.returncode == 0:
                    log = json.loads(log_result.stdout)
                    issues = log.get("issues", [])
                    if issues:
                        issue_summary = "\n" + "\n".join(
                            "- {severity}: {message} [{architecture}] {path}".format(
                                severity=issue.get("severity", "unknown"),
                                message=issue.get("message", "unknown issue"),
                                architecture=issue.get("architecture", "unknown"),
                                path=issue.get("path", "unknown path"),
                            )
                            for issue in issues
                        )
            raise SystemExit(
                "Apple notarization was not accepted: "
                f"{response.get('status', 'unknown')}"
                f" (submission {submission_id or 'unknown'}){issue_summary}"
            )
    print(f"Apple notarization accepted: {response.get('id', 'unknown')}")
    _verify_macos_notarization(executable)


def _write_binary_readme(path: Path, version: str, target: str) -> None:
    executable = "foresight-ocr.exe" if os.name == "nt" else "foresight-ocr"
    support = {
        "linux-x86_64": "Linux x86-64 with glibc 2.35 or newer",
        "linux-arm64": "Linux ARM64 with glibc 2.35 or newer",
        "macos-x86_64": "macOS 14 or newer on Intel",
        "macos-arm64": "macOS 14 or newer on Apple silicon",
        "windows-x86_64": "64-bit Windows 10 or Windows 11",
    }[target]
    path.write_text(
        f"""Foresight OCR {version} standalone CLI ({target})

Supported system: {support}.

Run `{executable} --help` from a terminal. The bundle includes the core Python
runtime and libraries; no separate Python installation is required.
Third-party license terms are reproduced in `THIRD_PARTY_NOTICES.txt`.

OCR models remain deliberately isolated. Create `.venv-paddle` or `.venv-vlm`
in the working project directory as described in the main README before using
those optional backends. Source PDFs, configuration, and generated artifacts
are never embedded in this archive.

The local review server binds to 127.0.0.1 by default and has no authentication.
Do not expose it directly to an untrusted network.
""",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", help="Expected platform label for CI verification.")
    args = parser.parse_args()

    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    version = project["project"]["version"]
    _verify_build_environment(project["project"])
    require_signing = _requires_signing(version)
    actual_target = _target()
    if args.target and args.target != actual_target:
        raise SystemExit(
            f"target mismatch: expected {args.target}, running on {actual_target}"
        )

    build_root = ROOT / "build" / "standalone"
    work_root = ROOT / "build" / "pyinstaller"
    add_data = [
        f"{ROOT / 'src' / 'foresight_ocr' / 'review' / 'app.html'}:foresight_ocr/review",
        f"{ROOT / 'runners' / 'ppocr_v5.py'}:foresight_ocr/backend_runners",
        f"{ROOT / 'runners' / 'paddleocr_vl.py'}:foresight_ocr/backend_runners",
    ]
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--clean",
        "--noconfirm",
        "--onedir",
        "--name",
        "foresight-ocr",
        "--paths",
        str(ROOT / "src"),
        "--distpath",
        str(build_root),
        "--workpath",
        str(work_root),
        "--specpath",
        str(work_root),
        "--copy-metadata",
        "foresight-ocr",
    ]
    for item in add_data:
        command.extend(["--add-data", item])
    command.append(str(ROOT / "src" / "foresight_ocr" / "__main__.py"))
    _run(command)

    executable = build_root / "foresight-ocr"
    if os.name == "nt":
        executable /= "foresight-ocr.exe"
    else:
        executable /= "foresight-ocr"
    if not executable.is_file():
        raise SystemExit(f"PyInstaller did not produce {executable}")

    if platform.system() == "Darwin":
        _verify_macos_floor(executable.parent)
        identity = os.environ.get("FORESIGHT_MACOS_SIGNING_IDENTITY")
        if identity:
            _sign_macos(executable.parent, identity)
        elif require_signing:
            raise SystemExit(
                "macOS 1.0 binaries require FORESIGHT_MACOS_SIGNING_IDENTITY"
            )
    elif platform.system() == "Windows":
        certificate_value = os.environ.get("FORESIGHT_WINDOWS_CERTIFICATE")
        password = os.environ.get("FORESIGHT_WINDOWS_CERTIFICATE_PASSWORD")
        timestamp = os.environ.get("FORESIGHT_WINDOWS_TIMESTAMP_URL")
        if certificate_value and password and timestamp:
            certificate = Path(certificate_value)
            if not certificate.is_file():
                raise SystemExit(
                    f"Windows signing certificate not found: {certificate}"
                )
            _sign_windows(executable.parent, certificate, password, timestamp)
        elif require_signing:
            raise SystemExit(
                "Windows 1.0 signing requires FORESIGHT_WINDOWS_CERTIFICATE, "
                "FORESIGHT_WINDOWS_CERTIFICATE_PASSWORD, and "
                "FORESIGHT_WINDOWS_TIMESTAMP_URL"
            )
    elif platform.system() == "Linux":
        _verify_linux_glibc_floor(executable.parent)

    _run(
        [
            sys.executable,
            str(ROOT / "scripts" / "smoke_cli_distribution.py"),
            str(executable),
            "--expected-version",
            version,
        ]
    )

    artifact_name = f"foresight-ocr-{version}-{actual_target}"
    stage = ROOT / "build" / "release" / artifact_name
    _copy_release_stage(
        executable.parent,
        stage,
        preserve_symlinks=platform.system() == "Darwin",
    )
    shutil.copy2(ROOT / "LICENSE", stage / "LICENSE")
    _write_binary_readme(stage / "README.txt", version, actual_target)
    _write_third_party_notices(stage / "THIRD_PARTY_NOTICES.txt")

    if platform.system() == "Darwin" and require_signing:
        _notarize_macos(stage, stage / "foresight-ocr")

    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    if os.name == "nt":
        archive = Path(
            shutil.make_archive(
                str(dist / artifact_name), "zip", stage.parent, stage.name
            )
        )
    else:
        archive = dist / f"{artifact_name}.tar.gz"
        with tarfile.open(archive, "w:gz") as output:
            output.add(stage, arcname=stage.name)

    _run(
        [
            sys.executable,
            str(ROOT / "scripts" / "verify_standalone_archive.py"),
            str(archive),
        ]
    )

    print(archive)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Run a checksum-pinned Gitleaks scan over Git history and publishable files."""

from __future__ import annotations

import hashlib
import platform
import shutil
import subprocess
import tarfile
import tempfile
import tomllib
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from audit_public_release import POLICY_NAME, ROOT, publishable_paths


def _safe_archive_name(name: str) -> None:
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts:
        raise RuntimeError(f"unsafe scanner archive path: {name!r}")


def _download_scanner(destination: Path) -> Path:
    policy = tomllib.loads((ROOT / POLICY_NAME).read_text(encoding="utf-8"))
    config = policy["gitleaks"]
    machine = platform.machine()
    if platform.system() == "Windows" and machine.lower() in {"amd64", "x86_64"}:
        machine = "AMD64"
    key = f"{platform.system()}-{machine}"
    try:
        asset = config["assets"][key]
    except KeyError as error:
        raise RuntimeError(f"no checksum-pinned Gitleaks asset for {key}") from error
    archive_name = asset["archive"]
    url = (
        "https://github.com/gitleaks/gitleaks/releases/download/"
        f"v{config['version']}/{archive_name}"
    )
    parsed_url = urlsplit(url)
    if parsed_url.scheme != "https" or parsed_url.netloc != "github.com":
        raise RuntimeError("refusing non-GitHub scanner URL")
    archive = destination / archive_name
    # The URL is constructed locally and its scheme and host are checked above.
    with (
        urllib.request.urlopen(url, timeout=60) as response,  # nosec B310
        archive.open("wb") as output,
    ):
        shutil.copyfileobj(response, output)
    actual = hashlib.sha256(archive.read_bytes()).hexdigest()
    if actual != asset["sha256"]:
        raise RuntimeError(
            f"Gitleaks checksum mismatch: expected {asset['sha256']}, got {actual}"
        )

    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as package:
            for info in package.infolist():
                _safe_archive_name(info.filename)
            try:
                member = package.getinfo("gitleaks.exe")
            except KeyError as error:
                raise RuntimeError(
                    "Gitleaks executable is missing from its archive"
                ) from error
            executable = destination / "gitleaks.exe"
            with package.open(member) as source, executable.open("wb") as output:
                shutil.copyfileobj(source, output)
    else:
        with tarfile.open(archive, "r:gz") as package:
            for member in package.getmembers():
                _safe_archive_name(member.name)
                if not (member.isfile() or member.isdir()):
                    raise RuntimeError(
                        f"special file in scanner archive: {member.name}"
                    )
            try:
                member = package.getmember("gitleaks")
            except KeyError as error:
                raise RuntimeError(
                    "Gitleaks executable is missing from its archive"
                ) from error
            source = package.extractfile(member)
            if source is None:
                raise RuntimeError("Gitleaks archive member is not a regular file")
            executable = destination / "gitleaks"
            with source, executable.open("wb") as output:
                shutil.copyfileobj(source, output)
        executable.chmod(0o755)
    if not executable.is_file():
        raise RuntimeError("Gitleaks executable is missing from its archive")
    return executable


def _run(executable: Path, *args: str) -> None:
    result = subprocess.run([str(executable), *args], cwd=ROOT, check=False)
    if result.returncode:
        raise SystemExit(result.returncode)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="foresight-ocr-gitleaks-") as temporary:
        temporary_root = Path(temporary)
        executable = _download_scanner(temporary_root)
        subprocess.run([str(executable), "version"], check=True)
        _run(
            executable, "git", "--no-banner", "--redact", "--log-opts=--all", str(ROOT)
        )

        tree = temporary_root / "publishable-tree"
        for path in publishable_paths(ROOT):
            source = ROOT / path
            if source.is_file() and not source.is_symlink():
                destination = tree / path
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, destination)
        _run(executable, "dir", "--no-banner", "--redact", str(tree))
    print("secret audit: Git history and publishable tree passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

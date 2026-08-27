#!/usr/bin/env python3
"""Build and smoke-test Linux AppImage, Debian, and RPM packages."""

from __future__ import annotations

import argparse
import hashlib
import os
import platform
import shutil
import stat
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APPIMAGETOOL_VERSION = "1.9.1"
APPIMAGE_RUNTIME_VERSION = "20251108"
TARGETS = {
    "linux-x86_64": {
        "deb_arch": "amd64",
        "rpm_arch": "x86_64",
        "appimage_arch": "x86_64",
        "appimagetool_sha256": (
            "ed4ce84f0d9caff66f50bcca6ff6f35aae54ce8135408b3fa33abfc3cb384eb0"
        ),
        "runtime_sha256": (
            "2fca8b443c92510f1483a883f60061ad09b46b978b2631c807cd873a47ec260d"
        ),
    },
    "linux-arm64": {
        "deb_arch": "arm64",
        "rpm_arch": "aarch64",
        "appimage_arch": "aarch64",
        "appimagetool_sha256": (
            "f0837e7448a0c1e4e650a93bb3e85802546e60654ef287576f46c71c126a9158"
        ),
        "runtime_sha256": (
            "00cbdfcf917cc6c0ff6d3347d59e0ca1f7f45a6df1a428a0d6d8a78664d87444"
        ),
    },
}


def _run(
    command: list[str],
    *,
    cwd: Path = ROOT,
    env: dict[str, str] | None = None,
) -> None:
    subprocess.run(command, cwd=cwd, env=env, check=True)


def _project() -> dict[str, object]:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = data["project"]
    assert isinstance(project, dict)
    return project


def _actual_target() -> str:
    machines = {"aarch64": "arm64", "x86_64": "x86_64"}
    if platform.system() != "Linux" or platform.machine() not in machines:
        raise SystemExit(
            f"Linux package builds do not support {platform.system()} "
            f"{platform.machine()}"
        )
    return f"linux-{machines[platform.machine()]}"


def _validate_stage(stage: Path, version: str, target: str) -> None:
    expected = f"foresight-ocr-{version}-{target}"
    if stage.name != expected:
        raise SystemExit(f"expected stage directory {expected}, received {stage.name}")
    required = {
        "foresight-ocr",
        "_internal",
        "LICENSE",
        "README.txt",
        "THIRD_PARTY_NOTICES.txt",
    }
    missing = sorted(name for name in required if not (stage / name).exists())
    if missing:
        raise SystemExit(f"standalone stage is incomplete: {missing}")
    if not os.access(stage / "foresight-ocr", os.X_OK):
        raise SystemExit(
            f"standalone launcher is not executable: {stage / 'foresight-ocr'}"
        )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download_verified(url: str, expected: str, destination: Path) -> Path:
    if destination.is_file() and _sha256(destination) == expected:
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".part")
    temporary.unlink(missing_ok=True)
    _run(
        [
            "curl",
            "--fail",
            "--location",
            "--proto",
            "=https",
            "--tlsv1.2",
            "--output",
            str(temporary),
            url,
        ]
    )
    actual = _sha256(temporary)
    if actual != expected:
        temporary.unlink(missing_ok=True)
        raise SystemExit(
            f"download checksum mismatch for {url}: expected {expected}, got {actual}"
        )
    temporary.replace(destination)
    return destination


def _smoke(
    executable: Path, version: str, *, env: dict[str, str] | None = None
) -> None:
    _run(
        [
            sys.executable,
            str(ROOT / "scripts" / "smoke_cli_distribution.py"),
            str(executable),
            "--expected-version",
            version,
        ],
        env=env,
    )


def _copy_payload(stage: Path, destination: Path) -> None:
    if destination.exists():
        shutil.rmtree(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(stage, destination, symlinks=True)


def _build_deb(
    stage: Path,
    version: str,
    target: str,
    work: Path,
    dist: Path,
) -> Path:
    metadata = TARGETS[target]
    architecture = str(metadata["deb_arch"])
    package_root = work / "deb-root"
    payload = package_root / "opt" / "foresight-ocr"
    _copy_payload(stage, payload)
    bin_dir = package_root / "usr" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "foresight-ocr").symlink_to("/opt/foresight-ocr/foresight-ocr")
    installed_size = sum(
        path.stat().st_size
        for path in payload.rglob("*")
        if path.is_file() and not path.is_symlink()
    )
    control_dir = package_root / "DEBIAN"
    control_dir.mkdir()
    control = control_dir / "control"
    control.write_text(
        "\n".join(
            [
                "Package: foresight-ocr",
                f"Version: {version}-1",
                f"Architecture: {architecture}",
                "Maintainer: Foresight OCR contributors",
                "Section: science",
                "Priority: optional",
                "Depends: libc6 (>= 2.35)",
                f"Installed-Size: {(installed_size + 1023) // 1024}",
                "Homepage: https://github.com/runkaiz/foresight-ocr",
                "Description: Provenance-first OCR for Chinese genealogy records",
                " Foresight OCR digitizes and reviews structured Chinese genealogy",
                " documents while preserving source evidence and human corrections.",
                "",
            ]
        ),
        encoding="utf-8",
    )
    control.chmod(0o644)
    artifact = dist / f"foresight-ocr_{version}-1_{architecture}.deb"
    _run(
        [
            "dpkg-deb",
            "--root-owner-group",
            "--build",
            str(package_root),
            str(artifact),
        ]
    )
    fields = subprocess.run(
        ["dpkg-deb", "--field", str(artifact), "Package", "Version", "Architecture"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    for expected in ("foresight-ocr", f"{version}-1", architecture):
        if expected not in fields:
            raise SystemExit(f"Debian metadata is missing {expected!r}: {fields}")
    extracted = work / "deb-extracted"
    _run(["dpkg-deb", "--extract", str(artifact), str(extracted)])
    _smoke(extracted / "opt" / "foresight-ocr" / "foresight-ocr", version)
    return artifact


def _rpm_spec(version: str, architecture: str) -> str:
    return f"""%global debug_package %{{nil}}
%global __os_install_post %{{nil}}
Name:           foresight-ocr
Version:        {version}
Release:        1
Summary:        Provenance-first OCR for Chinese genealogy records
License:        Apache-2.0
URL:            https://github.com/runkaiz/foresight-ocr
BuildArch:      {architecture}
AutoReqProv:    no
Requires:       glibc >= 2.35

%description
Foresight OCR digitizes and reviews structured Chinese genealogy documents
while preserving source evidence and human corrections.

%prep

%build

%install
rm -rf "%{{buildroot}}"
install -d "%{{buildroot}}/opt/foresight-ocr" "%{{buildroot}}/usr/bin"
cp -a "%{{_payload_dir}}/." "%{{buildroot}}/opt/foresight-ocr/"
ln -s /opt/foresight-ocr/foresight-ocr "%{{buildroot}}/usr/bin/foresight-ocr"

%files
%license /opt/foresight-ocr/LICENSE
%doc /opt/foresight-ocr/README.txt
%doc /opt/foresight-ocr/THIRD_PARTY_NOTICES.txt
/opt/foresight-ocr/foresight-ocr
/opt/foresight-ocr/_internal
/usr/bin/foresight-ocr

%changelog
* Thu Aug 27 2026 Foresight OCR contributors - {version}-1
- Automated upstream release package
"""


def _build_rpm(
    stage: Path,
    version: str,
    target: str,
    work: Path,
    dist: Path,
) -> Path:
    architecture = str(TARGETS[target]["rpm_arch"])
    topdir = work / "rpmbuild"
    for name in ("BUILD", "BUILDROOT", "RPMS", "SOURCES", "SPECS", "SRPMS"):
        (topdir / name).mkdir(parents=True)
    spec = topdir / "SPECS" / "foresight-ocr.spec"
    spec.write_text(_rpm_spec(version, architecture), encoding="utf-8")
    _run(
        [
            "rpmbuild",
            "-bb",
            str(spec),
            "--target",
            architecture,
            "--define",
            f"_topdir {topdir}",
            "--define",
            f"_payload_dir {stage}",
        ]
    )
    built = list((topdir / "RPMS").rglob("*.rpm"))
    if len(built) != 1:
        raise SystemExit(f"expected one RPM, found {[path.name for path in built]}")
    artifact = dist / f"foresight-ocr-{version}-1.{architecture}.rpm"
    shutil.copy2(built[0], artifact)
    query = subprocess.run(
        [
            "rpm",
            "-qp",
            "--queryformat",
            "%{NAME}\n%{VERSION}-%{RELEASE}\n%{ARCH}\n",
            str(artifact),
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    if query != ["foresight-ocr", f"{version}-1", architecture]:
        raise SystemExit(f"unexpected RPM metadata: {query}")

    extracted = work / "rpm-extracted"
    extracted.mkdir()
    rpm2cpio = subprocess.Popen(
        ["rpm2cpio", str(artifact)],
        stdout=subprocess.PIPE,
    )
    assert rpm2cpio.stdout is not None
    extraction = subprocess.run(
        ["cpio", "--extract", "--make-directories", "--quiet"],
        cwd=extracted,
        stdin=rpm2cpio.stdout,
        check=True,
    )
    rpm2cpio.stdout.close()
    if rpm2cpio.wait() != 0 or extraction.returncode != 0:
        raise SystemExit("RPM extraction failed")
    _smoke(extracted / "opt" / "foresight-ocr" / "foresight-ocr", version)
    return artifact


def _build_appimage(
    stage: Path,
    version: str,
    target: str,
    work: Path,
    dist: Path,
) -> Path:
    metadata = TARGETS[target]
    architecture = str(metadata["appimage_arch"])
    appdir = work / "Foresight_OCR.AppDir"
    payload = appdir / "usr" / "lib" / "foresight-ocr"
    _copy_payload(stage, payload)
    runtime_license = (
        ROOT / "packaging" / "linux" / "AppImage-runtime-LICENSE.txt"
    ).read_text(encoding="utf-8")
    with (payload / "THIRD_PARTY_NOTICES.txt").open("a", encoding="utf-8") as notices:
        notices.write("\n" + "=" * 79 + "\nAppImage type 2 runtime\n\n")
        notices.write(runtime_license)
    bin_dir = appdir / "usr" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "foresight-ocr").symlink_to("../lib/foresight-ocr/foresight-ocr")

    app_run = appdir / "AppRun"
    app_run.write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        'APPDIR=${APPDIR:-"$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"}\n'
        'exec "$APPDIR/usr/lib/foresight-ocr/foresight-ocr" "$@"\n',
        encoding="utf-8",
    )
    app_run.chmod(0o755)
    desktop = appdir / "foresight-ocr.desktop"
    desktop.write_text(
        """[Desktop Entry]
Type=Application
Name=Foresight OCR
Comment=Provenance-first OCR for Chinese genealogy records
Exec=foresight-ocr
Icon=foresight-ocr
Terminal=true
Categories=Utility;Science;
""",
        encoding="utf-8",
    )
    validator = shutil.which("desktop-file-validate")
    if validator:
        _run([validator, str(desktop)])
    applications = appdir / "usr" / "share" / "applications"
    applications.mkdir(parents=True)
    shutil.copy2(desktop, applications / desktop.name)
    icon = ROOT / "packaging" / "linux" / "foresight-ocr.svg"
    shutil.copy2(icon, appdir / icon.name)
    icons = appdir / "usr" / "share" / "icons" / "hicolor" / "scalable" / "apps"
    icons.mkdir(parents=True)
    shutil.copy2(icon, icons / icon.name)

    tools = ROOT / "build" / "release-tools"
    tool_name = f"appimagetool-{architecture}.AppImage"
    tool = _download_verified(
        "https://github.com/AppImage/appimagetool/releases/download/"
        f"{APPIMAGETOOL_VERSION}/{tool_name}",
        str(metadata["appimagetool_sha256"]),
        tools / tool_name,
    )
    runtime_name = f"runtime-{architecture}"
    runtime = _download_verified(
        "https://github.com/AppImage/type2-runtime/releases/download/"
        f"{APPIMAGE_RUNTIME_VERSION}/{runtime_name}",
        str(metadata["runtime_sha256"]),
        tools / runtime_name,
    )
    tool.chmod(tool.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    artifact = dist / f"foresight-ocr-{version}-{target}.AppImage"
    env = os.environ.copy()
    env.update(
        {
            "APPIMAGE_EXTRACT_AND_RUN": "1",
            "ARCH": architecture,
            "VERSION": version,
        }
    )
    _run(
        [str(tool), "--runtime-file", str(runtime), str(appdir), str(artifact)],
        env=env,
    )
    artifact.chmod(artifact.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    launched_version = subprocess.run(
        [str(artifact), "--version"],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if launched_version != version:
        raise SystemExit(
            f"AppImage version mismatch: expected {version}, got {launched_version}"
        )
    extracted_parent = work / "appimage-extracted"
    extracted_parent.mkdir()
    extraction_env = os.environ.copy()
    extraction_env.pop("APPIMAGE_EXTRACT_AND_RUN", None)
    _run(
        [str(artifact), "--appimage-extract"],
        cwd=extracted_parent,
        env=extraction_env,
    )
    extracted = extracted_parent / "squashfs-root"
    expected = (
        extracted / "AppRun",
        extracted / "foresight-ocr.desktop",
        extracted / "foresight-ocr.svg",
        extracted / "usr" / "lib" / "foresight-ocr" / "_internal",
    )
    missing = [
        str(path.relative_to(extracted)) for path in expected if not path.exists()
    ]
    if missing:
        raise SystemExit(f"AppImage is incomplete after extraction: {missing}")
    notices = (
        extracted / "usr" / "lib" / "foresight-ocr" / "THIRD_PARTY_NOTICES.txt"
    ).read_text(encoding="utf-8")
    if "AppImage type 2 runtime" not in notices or "2004-23 probonopd" not in notices:
        raise SystemExit("AppImage runtime notice is missing")
    _smoke(extracted / "AppRun", version)
    return artifact


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", required=True, choices=sorted(TARGETS))
    parser.add_argument("--stage", type=Path)
    args = parser.parse_args()

    project = _project()
    version = str(project["version"])
    actual_target = _actual_target()
    if args.target != actual_target:
        raise SystemExit(
            f"target mismatch: expected {args.target}, running on {actual_target}"
        )
    stage = args.stage or (
        ROOT / "build" / "release" / f"foresight-ocr-{version}-{args.target}"
    )
    stage = stage.resolve()
    _validate_stage(stage, version, args.target)
    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    (ROOT / "build").mkdir(exist_ok=True)

    with tempfile.TemporaryDirectory(
        prefix=f"foresight-ocr-{args.target}-", dir=ROOT / "build"
    ) as temporary:
        work = Path(temporary)
        artifacts = [
            _build_deb(stage, version, args.target, work / "deb", dist),
            _build_rpm(stage, version, args.target, work / "rpm", dist),
            _build_appimage(stage, version, args.target, work / "appimage", dist),
        ]
    for artifact in artifacts:
        print(artifact)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

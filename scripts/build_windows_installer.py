#!/usr/bin/env python3
"""Build, validate, sign, and smoke-test the Windows MSI installer."""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UPGRADE_CODE = "D90362C4-3C78-5EDF-9E55-43E774A8680F"
PATH_COMPONENT_GUID = "B8102987-2B25-5684-93AE-6153FB0C0758"


def _run(command: list[str], *, cwd: Path = ROOT) -> None:
    subprocess.run(command, cwd=cwd, check=True)


def _version() -> str:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return str(project["project"]["version"])


def _requires_signing(version: str) -> bool:
    return (
        int(version.split(".", 1)[0]) >= 1
        or os.environ.get("FORESIGHT_REQUIRE_SIGNING") == "1"
    )


def _write_signing_status(dist: Path, version: str, *, signed: bool) -> Path:
    path = dist / f"foresight-ocr-{version}-windows-x86_64-signing.json"
    document = {
        "schema_version": 1,
        "version": version,
        "target": "windows-x86_64",
        "authenticode": "signed" if signed else "unsigned",
        "artifacts": [
            f"foresight-ocr-{version}-windows-x86_64.msi",
            f"foresight-ocr-{version}-windows-x86_64.zip",
        ],
    }
    path.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return path


def _wxs(version: str) -> str:
    return f"""<?xml version="1.0" encoding="utf-8"?>
<Wix xmlns="http://wixtoolset.org/schemas/v4/wxs">
  <Package
      Name="Foresight OCR"
      Manufacturer="Foresight OCR contributors"
      Version="{version}"
      UpgradeCode="{UPGRADE_CODE}"
      Scope="perMachine">
    <SummaryInformation
        Description="Provenance-first OCR for Chinese genealogy records"
        Manufacturer="Foresight OCR contributors" />
    <MajorUpgrade
        DowngradeErrorMessage="A newer version of Foresight OCR is already installed." />
    <MediaTemplate EmbedCab="yes" CompressionLevel="high" />

    <StandardDirectory Id="ProgramFiles64Folder">
      <Directory Id="INSTALLFOLDER" Name="Foresight OCR">
        <Files Include="$(var.Payload)\\**" />
        <Component
            Id="PathEnvironment"
            Guid="{PATH_COMPONENT_GUID}"
            KeyPath="yes">
          <Environment
              Id="ForesightOCRPath"
              Name="PATH"
              Value="[INSTALLFOLDER]"
              Action="set"
              Part="last"
              Permanent="no"
              System="yes" />
        </Component>
      </Directory>
    </StandardDirectory>
  </Package>
</Wix>
"""


def _validate_stage(stage: Path, version: str) -> None:
    expected = f"foresight-ocr-{version}-windows-x86_64"
    if stage.name != expected:
        raise SystemExit(f"expected stage directory {expected}, received {stage.name}")
    required = (
        "foresight-ocr.exe",
        "_internal",
        "LICENSE",
        "README.txt",
        "THIRD_PARTY_NOTICES.txt",
    )
    missing = sorted(name for name in required if not (stage / name).exists())
    if missing:
        raise SystemExit(f"standalone stage is incomplete: {missing}")


def _sign(artifact: Path) -> None:
    certificate_value = os.environ.get("FORESIGHT_WINDOWS_CERTIFICATE")
    password = os.environ.get("FORESIGHT_WINDOWS_CERTIFICATE_PASSWORD")
    timestamp = os.environ.get("FORESIGHT_WINDOWS_TIMESTAMP_URL")
    signtool = os.environ.get("FORESIGHT_SIGNTOOL")
    if not all((certificate_value, password, timestamp, signtool)):
        raise SystemExit(
            "MSI signing requires FORESIGHT_WINDOWS_CERTIFICATE, "
            "FORESIGHT_WINDOWS_CERTIFICATE_PASSWORD, "
            "FORESIGHT_WINDOWS_TIMESTAMP_URL, and FORESIGHT_SIGNTOOL"
        )
    assert certificate_value and password and timestamp and signtool
    certificate = Path(certificate_value)
    if not certificate.is_file() or not Path(signtool).is_file():
        raise SystemExit("Windows signing certificate or SignTool was not found")
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
            str(artifact),
        ]
    )
    _run([signtool, "verify", "/pa", "/v", str(artifact)])


def _smoke_installed(artifact: Path, version: str) -> None:
    with tempfile.TemporaryDirectory(prefix="foresight-ocr-msi-") as temporary:
        destination = Path(temporary) / "image"
        log = Path(temporary) / "msiexec.log"
        result = subprocess.run(
            [
                "msiexec.exe",
                "/a",
                str(artifact),
                "/qn",
                f"TARGETDIR={destination}",
                "/l*v",
                str(log),
            ],
            check=False,
        )
        if result.returncode not in {0, 1641, 3010}:
            details = log.read_text(encoding="utf-16", errors="replace")
            raise SystemExit(
                f"administrative MSI install failed ({result.returncode}):\n{details}"
            )
        candidates = [
            path
            for path in destination.rglob("foresight-ocr.exe")
            if (path.parent / "_internal").is_dir()
        ]
        if len(candidates) != 1:
            raise SystemExit(
                f"expected one installed launcher, found {[str(path) for path in candidates]}"
            )
        _run(
            [
                sys.executable,
                str(ROOT / "scripts" / "smoke_cli_distribution.py"),
                str(candidates[0]),
                "--expected-version",
                version,
            ]
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", type=Path)
    parser.add_argument("--wix", default="wix")
    args = parser.parse_args()

    if platform.system() != "Windows" or platform.machine() not in {
        "AMD64",
        "x86_64",
    }:
        raise SystemExit(
            f"Windows MSI builds do not support {platform.system()} {platform.machine()}"
        )
    version = _version()
    stage = args.stage or (
        ROOT / "build" / "release" / f"foresight-ocr-{version}-windows-x86_64"
    )
    stage = stage.resolve()
    _validate_stage(stage, version)

    work = ROOT / "build" / "windows-installer"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    source = work / "foresight-ocr.wxs"
    source.write_text(_wxs(version), encoding="utf-8")
    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    artifact = dist / f"foresight-ocr-{version}-windows-x86_64.msi"
    _run(
        [
            args.wix,
            "build",
            "-arch",
            "x64",
            "-d",
            f"Payload={stage}",
            "-pdbtype",
            "none",
            "-out",
            str(artifact),
            str(source),
        ]
    )
    _run([args.wix, "msi", "validate", str(artifact)])

    signing_values = (
        os.environ.get("FORESIGHT_WINDOWS_CERTIFICATE"),
        os.environ.get("FORESIGHT_WINDOWS_CERTIFICATE_PASSWORD"),
        os.environ.get("FORESIGHT_WINDOWS_TIMESTAMP_URL"),
        os.environ.get("FORESIGHT_SIGNTOOL"),
    )
    signed = all(signing_values)
    if any(signing_values) and not signed:
        raise SystemExit("Windows signing configuration is incomplete")
    if signed:
        _sign(artifact)
    elif _requires_signing(version):
        _sign(artifact)
    _smoke_installed(artifact, version)
    status = _write_signing_status(dist, version, signed=signed)
    print(artifact)
    print(status)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

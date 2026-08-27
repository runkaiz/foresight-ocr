from __future__ import annotations

import hashlib
import importlib.util
import io
import sys
import tarfile
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest


def _load_script(name: str):
    path = Path(__file__).parents[1] / "scripts" / name
    spec = importlib.util.spec_from_file_location(name.removesuffix(".py"), path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def linux_packager():
    return _load_script("build_linux_packages.py")


@pytest.fixture
def aur_packager():
    return _load_script("build_aur_package.py")


@pytest.fixture
def windows_packager():
    return _load_script("build_windows_installer.py")


@pytest.fixture
def release_policy():
    return _load_script("release_signing_policy.py")


@pytest.fixture
def windows_status_verifier():
    return _load_script("verify_windows_signing_status.py")


def _standalone_stage(root: Path, target: str, *, windows: bool = False) -> Path:
    stage = root / f"foresight-ocr-0.1.0-{target}"
    stage.mkdir()
    launcher = stage / ("foresight-ocr.exe" if windows else "foresight-ocr")
    launcher.write_bytes(b"launcher")
    launcher.chmod(0o755)
    (stage / "_internal").mkdir()
    (stage / "LICENSE").write_text("license", encoding="utf-8")
    (stage / "README.txt").write_text(target, encoding="utf-8")
    (stage / "THIRD_PARTY_NOTICES.txt").write_text("notices", encoding="utf-8")
    return stage


def _release_archive(path: Path, target: str) -> None:
    root = f"foresight-ocr-0.1.0-{target}"
    with tarfile.open(path, "w:gz") as archive:
        for name in ("foresight-ocr", "LICENSE", "THIRD_PARTY_NOTICES.txt"):
            payload = name.encode()
            info = tarfile.TarInfo(f"{root}/{name}")
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))


def test_linux_package_metadata_and_stage_contract(
    linux_packager, tmp_path: Path
) -> None:
    stage = _standalone_stage(tmp_path, "linux-x86_64")
    linux_packager._validate_stage(stage, "0.1.0", "linux-x86_64")

    spec = linux_packager._rpm_spec("0.1.0", "x86_64")
    assert "Requires:       glibc >= 2.35" in spec
    assert "/opt/foresight-ocr/_internal" in spec
    assert "%global __os_install_post %{nil}" in spec
    assert linux_packager.TARGETS["linux-arm64"]["deb_arch"] == "arm64"
    assert linux_packager.TARGETS["linux-arm64"]["rpm_arch"] == "aarch64"


def test_linux_stage_rejects_missing_runtime(linux_packager, tmp_path: Path) -> None:
    stage = _standalone_stage(tmp_path, "linux-x86_64")
    (stage / "_internal").rmdir()
    with pytest.raises(SystemExit, match="_internal"):
        linux_packager._validate_stage(stage, "0.1.0", "linux-x86_64")


def test_aur_recipe_uses_release_archives_and_exact_hashes(
    aur_packager, tmp_path: Path
) -> None:
    checksums = {}
    for target in aur_packager.TARGETS:
        path = tmp_path / f"foresight-ocr-0.1.0-{target}.tar.gz"
        _release_archive(path, target)
        aur_packager._verify_archive(path, "0.1.0", target)
        checksums[target] = hashlib.sha256(path.read_bytes()).hexdigest()

    pkgbuild = aur_packager._pkgbuild("0.1.0", checksums)
    srcinfo = aur_packager._srcinfo("0.1.0", checksums)
    assert "pkgname=foresight-ocr-bin" in pkgbuild
    assert "arch=('x86_64' 'aarch64')" in pkgbuild
    assert "options=('!strip')" in pkgbuild
    assert "v${pkgver}/foresight-ocr-0.1.0-linux-x86_64.tar.gz" in pkgbuild
    assert checksums["linux-x86_64"] in pkgbuild
    assert checksums["linux-arm64"] in srcinfo
    assert "v0.1.0/foresight-ocr-0.1.0-linux-x86_64.tar.gz" in srcinfo
    assert "${pkgver}" not in srcinfo
    assert "pkgname = foresight-ocr-bin" in srcinfo


def test_windows_installer_contract(windows_packager, tmp_path: Path) -> None:
    stage = _standalone_stage(tmp_path, "windows-x86_64", windows=True)
    windows_packager._validate_stage(stage, "0.1.0")

    root = ET.fromstring(windows_packager._wxs("0.1.0"))
    namespace = {"w": "http://wixtoolset.org/schemas/v4/wxs"}
    package = root.find("w:Package", namespace)
    assert package is not None
    assert package.attrib["Scope"] == "perMachine"
    assert package.attrib["UpgradeCode"] == windows_packager.UPGRADE_CODE
    files = root.find(".//w:Files", namespace)
    environment = root.find(".//w:Environment", namespace)
    component = root.find(".//w:Component", namespace)
    assert files is not None and files.attrib["Include"] == "$(var.Payload)\\**"
    assert component is not None
    assert component.attrib["Guid"] == windows_packager.PATH_COMPONENT_GUID
    assert component.attrib["KeyPath"] == "yes"
    assert environment is not None
    assert environment.attrib == {
        "Id": "ForesightOCRPath",
        "Name": "PATH",
        "Value": "[INSTALLFOLDER]",
        "Action": "set",
        "Part": "last",
        "Permanent": "no",
        "System": "yes",
    }


def test_windows_installer_signing_gate(
    windows_packager, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("FORESIGHT_REQUIRE_SIGNING", raising=False)
    assert not windows_packager._requires_signing("0.9.9")
    assert windows_packager._requires_signing("1.0.0")
    monkeypatch.setenv("FORESIGHT_REQUIRE_SIGNING", "1")
    assert windows_packager._requires_signing("0.1.0")


@pytest.mark.parametrize(
    (
        "ref_type",
        "ref_name",
        "signed_test",
        "apple_signed_test",
        "require_windows",
        "expected",
    ),
    [
        ("branch", "main", "false", "false", "false", (False, False)),
        ("branch", "main", "true", "false", "false", (True, True)),
        ("branch", "main", "false", "true", "false", (True, False)),
        ("branch", "main", "false", "false", "true", (False, True)),
        ("tag", "v0.1.0", "false", "false", "false", (True, False)),
        ("tag", "v1.0.0", "false", "false", "false", (True, True)),
        ("tag", "v2.3.4", "false", "false", "false", (True, True)),
    ],
)
def test_release_signing_policy(
    release_policy,
    ref_type: str,
    ref_name: str,
    signed_test: str,
    apple_signed_test: str,
    require_windows: str,
    expected: tuple[bool, bool],
) -> None:
    policy = release_policy.resolve_policy(
        ref_type=ref_type,
        ref_name=ref_name,
        signed_test=signed_test,
        apple_signed_test=apple_signed_test,
        require_windows_signing=require_windows,
    )
    assert (policy.apple_required, policy.windows_required) == expected


def test_release_signing_policy_rejects_invalid_configuration(release_policy) -> None:
    with pytest.raises(ValueError, match="must be true or false"):
        release_policy.resolve_policy(
            ref_type="branch",
            ref_name="main",
            require_windows_signing="sometimes",
        )
    with pytest.raises(ValueError, match="cannot determine major version"):
        release_policy.resolve_policy(ref_type="tag", ref_name="release-0.1.0")


@pytest.mark.parametrize("signed", [False, True])
def test_windows_signing_disclosure_matches_built_artifacts(
    windows_packager,
    windows_status_verifier,
    tmp_path: Path,
    signed: bool,
) -> None:
    version = "0.1.0"
    for suffix in ("msi", "zip"):
        (tmp_path / f"foresight-ocr-{version}-windows-x86_64.{suffix}").touch()
    status = windows_packager._write_signing_status(tmp_path, version, signed=signed)

    expected = "signed" if signed else "unsigned"
    windows_status_verifier.verify(status, expected)
    with pytest.raises(AssertionError, match="disclosure mismatch"):
        windows_status_verifier.verify(status, "unsigned" if signed else "signed")


def test_release_workflow_has_non_publishing_signed_test() -> None:
    workflow = (
        Path(__file__).parents[1] / ".github" / "workflows" / "release.yml"
    ).read_text(encoding="utf-8")

    assert "signed_test:" in workflow
    assert "apple_signed_test:" in workflow
    assert "name: Validate signing configuration" in workflow
    assert "inputs.signed_test == true" in workflow
    assert "inputs.apple_signed_test == true" in workflow
    assert "APPLE_SIGNING_REQUIRED:" in workflow
    assert "WINDOWS_SIGNING_REQUIRED:" in workflow
    assert "scripts/release_signing_policy.py" in workflow
    assert "vars.REQUIRE_WINDOWS_SIGNING" in workflow
    assert "steps.policy.outputs.apple_signing_required" in workflow
    assert "steps.policy.outputs.windows_signing_required" in workflow
    assert "needs.signing-preflight.outputs.windows_signing_required" in workflow
    assert "PUBLICATION_AUDIT_REQUIRED:" in workflow
    assert "scripts/verify_windows_signing_status.py" in workflow
    assert "windows-x86_64-signing.json" in workflow
    assert "name: Run native application tests" in workflow
    assert "startsWith(matrix.target, 'macos-')" in workflow
    assert "FORESIGHT_REQUIRE_SIGNING:" in workflow
    assert (
        workflow.count("if: github.event_name == 'push' && github.ref_type == 'tag'")
        == 3
    )


def test_ci_runs_native_macos_tests() -> None:
    workflow = (
        Path(__file__).parents[1] / ".github" / "workflows" / "ci.yml"
    ).read_text(encoding="utf-8")

    assert "name: Test native macOS application" in workflow
    assert "clients/macos/scripts/test.sh" in workflow


def test_macos_release_builder_uses_only_system_file_matcher() -> None:
    script = (
        Path(__file__).parents[1] / "clients" / "macos" / "scripts" / "build-app.sh"
    ).read_text(encoding="utf-8")

    assert "| /usr/bin/grep " in script
    assert "| rg " not in script

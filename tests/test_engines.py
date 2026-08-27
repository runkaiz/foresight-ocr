from __future__ import annotations

import json
from pathlib import Path

import pytest

from foresight_ocr.ocr import engines
from foresight_ocr.persistence.locks import stage_lock


def _configure_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "engine-home"
    uv = tmp_path / "uv"
    uv.write_text("fake installer", encoding="utf-8")
    monkeypatch.setenv("FORESIGHT_OCR_ENGINE_HOME", str(home))
    monkeypatch.setenv("FORESIGHT_OCR_UV", str(uv))
    return home


def _write_fake_python(environment: Path, contents: str) -> Path:
    python = engines._python_path(environment)
    python.parent.mkdir(parents=True)
    python.write_text(contents, encoding="utf-8")
    return python


def test_engine_manifest_reports_missing_shared_installation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _configure_home(tmp_path, monkeypatch)

    manifest = engines.engine_manifest()

    assert manifest["engine_home"] == str(home)
    assert manifest["installer_available"] is True
    statuses = {item["name"]: item for item in manifest["engines"]}
    assert statuses["ppocr_v5"]["state"] == "not_installed"
    assert statuses["ppocr_v5"]["requirements"] == (
        "paddlepaddle==3.3.1",
        "paddleocr==3.7.0",
    )


def test_install_engine_builds_then_atomically_publishes_managed_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _configure_home(tmp_path, monkeypatch)
    events: list[tuple[str, str]] = []

    def fake_run(
        command: list[str],
        environment: dict[str, str],
        event: engines.EngineEvent,
        stage: str,
    ) -> None:
        del environment
        event(stage, "started")
        if stage == "python_runtime":
            target = Path(command[-1])
            _write_fake_python(target, "fake python")
        event(stage, "completed")

    expected = {"paddlepaddle": "3.3.1", "paddleocr": "3.7.0"}
    monkeypatch.setattr(engines, "_run_install_command", fake_run)
    monkeypatch.setattr(engines, "_installed_versions", lambda *_args: expected)

    status = engines.install_engine(
        "ppocr_v5", event=lambda *event: events.append(event)
    )

    target = home / "ppocr-v5"
    marker = json.loads((target / engines.ENGINE_MANIFEST).read_text(encoding="utf-8"))
    assert status.available is True
    assert status.managed is True
    assert marker["managed"] is True
    assert marker["requirements"] == [
        "paddlepaddle==3.3.1",
        "paddleocr==3.7.0",
    ]
    assert events == [
        ("python_runtime", "started"),
        ("python_runtime", "completed"),
        ("engine_packages", "started"),
        ("engine_packages", "completed"),
        ("engine_probe", "started"),
        ("engine_probe", "completed"),
    ]
    assert not list(home.glob(".*.installing-*"))


def test_install_engine_never_replaces_unmanaged_environment_without_opt_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _configure_home(tmp_path, monkeypatch)
    target = home / "ppocr-v5"
    target.mkdir(parents=True)
    keep = target / "keep.txt"
    keep.write_text("user environment", encoding="utf-8")

    with pytest.raises(engines.EngineInstallError, match="unmanaged"):
        engines.install_engine("ppocr_v5")

    assert keep.read_text(encoding="utf-8") == "user environment"


def test_failed_upgrade_leaves_existing_managed_environment_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _configure_home(tmp_path, monkeypatch)
    target = home / "ppocr-v5"
    _write_fake_python(target, "old")
    (target / engines.ENGINE_MANIFEST).write_text(
        json.dumps({"managed": True}), encoding="utf-8"
    )
    keep = target / "keep.txt"
    keep.write_text("last known good", encoding="utf-8")
    monkeypatch.setattr(engines, "_installed_versions", lambda *_args: {})

    def fail_install(*_args, **_kwargs) -> None:
        raise engines.EngineInstallError("simulated network failure")

    monkeypatch.setattr(engines, "_run_install_command", fail_install)

    with pytest.raises(engines.EngineInstallError, match="network failure"):
        engines.install_engine("ppocr_v5")

    assert keep.read_text(encoding="utf-8") == "last known good"
    assert not list(home.glob(".*.installing-*"))


def test_install_recovers_last_environment_after_interrupted_atomic_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _configure_home(tmp_path, monkeypatch)
    backup = home / ".ppocr-v5.previous"
    _write_fake_python(backup, "last good")
    (backup / engines.ENGINE_MANIFEST).write_text(
        json.dumps({"managed": True}), encoding="utf-8"
    )
    expected = {"paddlepaddle": "3.3.1", "paddleocr": "3.7.0"}
    monkeypatch.setattr(engines, "_installed_versions", lambda *_args: expected)

    status = engines.install_engine("ppocr_v5")

    target = home / "ppocr-v5"
    assert status.available is True
    assert engines._python_path(target).read_text(encoding="utf-8") == "last good"
    assert not backup.exists()


def test_install_refuses_concurrent_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _configure_home(tmp_path, monkeypatch)

    with stage_lock(home, "engine-ppocr_v5", "test-holder"):
        with pytest.raises(engines.EngineInstallError, match="already running"):
            engines.install_engine("ppocr_v5")

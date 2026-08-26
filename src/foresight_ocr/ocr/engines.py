"""Managed, shared OCR-engine environments for desktop and CLI clients.

The core application ships its own Python runtime.  Optional OCR libraries stay
in isolated environments because PaddleOCR and mlx-vlm have incompatible
dependency graphs.  Those environments live in user application data rather
than inside each document project, so one installation can serve every project.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from foresight_ocr import __version__

ENGINE_MANIFEST = "foresight-ocr-engine.json"
ENGINE_PROTOCOL_VERSION = 1
PYTHON_VERSION = "3.12"
EngineEvent = Callable[[str, str], None]


class EngineInstallError(RuntimeError):
    """An OCR engine could not be installed without risking user data."""


@dataclass(frozen=True)
class EngineSpec:
    name: str
    display_name: str
    environment_directory: str
    runner: str
    requirements: tuple[str, ...]
    supported_targets: tuple[str, ...] = ()

    def supports_current_platform(self) -> bool:
        if not self.supported_targets:
            return True
        return _platform_target() in self.supported_targets


@dataclass(frozen=True)
class EngineStatus:
    name: str
    display_name: str
    state: str
    available: bool
    supported: bool
    detail: str
    environment: str
    requirements: tuple[str, ...]
    installed_versions: dict[str, str]
    managed: bool

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


ENGINE_SPECS: dict[str, EngineSpec] = {
    "ppocr_v5": EngineSpec(
        name="ppocr_v5",
        display_name="PP-OCRv5",
        environment_directory="ppocr-v5",
        runner="ppocr_v5.py",
        requirements=("paddlepaddle==3.3.1", "paddleocr==3.7.0"),
    ),
    "paddleocr_vl": EngineSpec(
        name="paddleocr_vl",
        display_name="PaddleOCR-VL",
        environment_directory="paddleocr-vl",
        runner="paddleocr_vl.py",
        requirements=("mlx-vlm==0.6.13",),
        supported_targets=("macos-arm64",),
    ),
}


def _platform_target() -> str:
    systems = {"Darwin": "macos", "Linux": "linux", "Windows": "windows"}
    machines = {
        "arm64": "arm64",
        "aarch64": "arm64",
        "x86_64": "x86_64",
        "AMD64": "x86_64",
    }
    return f"{systems.get(platform.system(), platform.system().lower())}-" f"{machines.get(platform.machine(), platform.machine().lower())}"


def engine_home() -> Path:
    override = os.environ.get("FORESIGHT_OCR_ENGINE_HOME")
    if override:
        return Path(override).expanduser().resolve()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Foresight OCR" / "engines"
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return base / "Foresight OCR" / "engines"
    base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "foresight-ocr" / "engines"


def engine_environment(spec: EngineSpec | str) -> Path:
    resolved = ENGINE_SPECS[spec] if isinstance(spec, str) else spec
    return engine_home() / resolved.environment_directory


def _python_path(environment: Path) -> Path:
    if os.name == "nt":
        return environment / "Scripts" / "python.exe"
    return environment / "bin" / "python"


def _runner_path(filename: str) -> Path:
    bundled = Path(__file__).resolve().parents[1] / "backend_runners" / filename
    if bundled.is_file():
        return bundled
    checkout = Path(__file__).resolve().parents[3] / "runners" / filename
    if checkout.is_file():
        return checkout
    return bundled


def _requirement_versions(requirements: tuple[str, ...]) -> dict[str, str]:
    return {
        requirement.split("==", 1)[0]: requirement.split("==", 1)[1]
        for requirement in requirements
    }


def _installed_versions(python: Path, names: tuple[str, ...]) -> dict[str, str]:
    script = (
        "import importlib.metadata,json;"
        f"names={json.dumps(names)};"
        "print(json.dumps({n:importlib.metadata.version(n) for n in names}))"
    )
    try:
        result = subprocess.run(
            [str(python), "-c", script],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}
    if result.returncode != 0:
        return {}
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {}
    if not isinstance(payload, dict):
        return {}
    return {str(name): str(version) for name, version in payload.items()}


def engine_status(name: str) -> EngineStatus:
    try:
        spec = ENGINE_SPECS[name]
    except KeyError as exc:
        raise EngineInstallError(f"unknown OCR engine: {name}") from exc
    environment = engine_environment(spec)
    supported = spec.supports_current_platform()

    def result(
        *,
        state: str,
        available: bool,
        detail: str,
        installed_versions: dict[str, str],
        managed: bool,
    ) -> EngineStatus:
        return EngineStatus(
            name=spec.name,
            display_name=spec.display_name,
            state=state,
            available=available,
            supported=supported,
            detail=detail,
            environment=str(environment),
            requirements=spec.requirements,
            installed_versions=installed_versions,
            managed=managed,
        )

    if not supported:
        return result(
            state="unsupported",
            available=False,
            detail=f"not available on {_platform_target()}",
            installed_versions={},
            managed=False,
        )
    if not environment.exists():
        return result(
            state="not_installed",
            available=False,
            detail="not installed",
            installed_versions={},
            managed=False,
        )

    marker = environment / ENGINE_MANIFEST
    managed = False
    if marker.is_file():
        try:
            payload = json.loads(marker.read_text(encoding="utf-8"))
            managed = payload.get("managed") is True
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return result(
                state="broken",
                available=False,
                detail="managed-engine manifest is invalid",
                installed_versions={},
                managed=False,
            )

    python = _python_path(environment)
    if not python.is_file():
        return result(
            state="broken",
            available=False,
            detail="Python interpreter is missing",
            installed_versions={},
            managed=managed,
        )
    required = _requirement_versions(spec.requirements)
    installed = _installed_versions(python, tuple(required))
    if installed != required:
        rendered = ", ".join(
            f"{package} {installed.get(package, 'missing')} (need {version})"
            for package, version in required.items()
        )
        return result(
            state="needs_install",
            available=False,
            detail=rendered,
            installed_versions=installed,
            managed=managed,
        )
    return result(
        state="ready",
        available=True,
        detail=", ".join(f"{key} {value}" for key, value in installed.items()),
        installed_versions=installed,
        managed=managed,
    )


def engine_manifest() -> dict[str, object]:
    installer = find_uv()
    return {
        "protocol_version": ENGINE_PROTOCOL_VERSION,
        "engine_home": str(engine_home()),
        "installer_available": installer is not None,
        "installer": str(installer) if installer is not None else None,
        "platform": _platform_target(),
        "engines": [engine_status(name).to_dict() for name in ENGINE_SPECS],
    }


def find_uv() -> Path | None:
    override = os.environ.get("FORESIGHT_OCR_UV")
    if override:
        candidate = Path(override).expanduser().resolve()
        return candidate if candidate.is_file() else None
    executable = Path(sys.executable).resolve()
    candidates: list[Path] = []
    if len(executable.parents) >= 3:
        candidates.append(executable.parents[2] / "Tools" / "uv")
    discovered = shutil.which("uv")
    if discovered:
        candidates.append(Path(discovered))
    return next((candidate for candidate in candidates if candidate.is_file()), None)


def _install_environment() -> dict[str, str]:
    environment = os.environ.copy()
    home = engine_home().parent
    environment.setdefault("UV_PYTHON_INSTALL_DIR", str(home / "python"))
    environment.setdefault("UV_CACHE_DIR", str(home / "cache" / "uv"))
    environment.setdefault("UV_NO_PROGRESS", "1")
    return environment


def _run_install_command(
    command: list[str],
    environment: dict[str, str],
    event: EngineEvent,
    stage: str,
) -> None:
    event(stage, "started")
    try:
        result = subprocess.run(
            command,
            env=environment,
            capture_output=True,
            text=True,
            timeout=3600,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise EngineInstallError(f"{stage} failed: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()[-1200:]
        raise EngineInstallError(f"{stage} failed: {detail or result.returncode}")
    event(stage, "completed")


def install_engine(
    name: str,
    *,
    replace: bool = False,
    event: EngineEvent | None = None,
) -> EngineStatus:
    """Install one engine into an atomic, app-managed shared environment."""
    try:
        spec = ENGINE_SPECS[name]
    except KeyError as exc:
        raise EngineInstallError(f"unknown OCR engine: {name}") from exc
    if not spec.supports_current_platform():
        raise EngineInstallError(
            f"{spec.display_name} is not available on {_platform_target()}"
        )
    uv = find_uv()
    if uv is None:
        raise EngineInstallError("the bundled uv installer could not be found")
    from foresight_ocr.persistence.locks import StageBusy, stage_lock

    try:
        with stage_lock(engine_home(), f"engine-{spec.name}", "engine-install"):
            return _install_engine_locked(spec, uv, replace=replace, event=event)
    except StageBusy as exc:
        raise EngineInstallError(
            f"another {spec.display_name} installation is already running"
        ) from exc


def _recover_interrupted_install(spec: EngineSpec) -> None:
    """Restore the last published environment after a killed atomic swap."""
    target = engine_environment(spec)
    backup = target.parent / f".{target.name}.previous"
    if backup.exists():
        if target.exists() and engine_status(spec.name).available:
            shutil.rmtree(backup)
        else:
            if target.exists():
                shutil.rmtree(target)
            backup.replace(target)
    for stale in target.parent.glob(f".{target.name}.installing-*"):
        if stale.is_dir():
            shutil.rmtree(stale)
        else:
            stale.unlink(missing_ok=True)


def _install_engine_locked(
    spec: EngineSpec,
    uv: Path,
    *,
    replace: bool,
    event: EngineEvent | None,
) -> EngineStatus:
    """Install while the per-engine advisory lock is held."""
    target = engine_environment(spec)
    target.parent.mkdir(parents=True, exist_ok=True)
    _recover_interrupted_install(spec)
    current = engine_status(spec.name)
    if current.available:
        return current
    if target.exists() and not current.managed and not replace:
        raise EngineInstallError(
            f"refusing to replace an unmanaged environment at {target}; "
            "pass --replace to opt in"
        )

    notify = event or (lambda _stage, _status: None)
    temporary = Path(
        tempfile.mkdtemp(prefix=f".{target.name}.installing-", dir=target.parent)
    )
    backup = target.parent / f".{target.name}.previous"
    environment = _install_environment()
    try:
        _run_install_command(
            [
                str(uv),
                "--no-config",
                "venv",
                "--managed-python",
                "--python",
                PYTHON_VERSION,
                str(temporary),
            ],
            environment,
            notify,
            "python_runtime",
        )
        python = _python_path(temporary)
        _run_install_command(
            [
                str(uv),
                "--no-config",
                "pip",
                "install",
                "--python",
                str(python),
                *spec.requirements,
            ],
            environment,
            notify,
            "engine_packages",
        )
        _run_install_command(
            [str(python), str(_runner_path(spec.runner)), "--probe"],
            environment,
            notify,
            "engine_probe",
        )
        marker = {
            "format_version": 1,
            "managed": True,
            "engine": spec.name,
            "requirements": list(spec.requirements),
            "installed_at": datetime.now(timezone.utc).isoformat(),
            "installed_with": __version__,
            "platform": _platform_target(),
        }
        (temporary / ENGINE_MANIFEST).write_text(
            json.dumps(marker, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        if target.exists():
            target.replace(backup)
        temporary.replace(target)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        if backup.exists():
            if target.exists():
                shutil.rmtree(target)
            backup.replace(target)
        raise

    status = engine_status(spec.name)
    if not status.available:
        if target.exists():
            shutil.rmtree(target)
        if backup.exists():
            backup.replace(target)
        raise EngineInstallError(
            f"installed engine did not validate: {status.detail}"
        )
    if backup.exists():
        shutil.rmtree(backup)
    return status

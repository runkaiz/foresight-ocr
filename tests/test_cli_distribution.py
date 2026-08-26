"""Distribution-facing CLI and backend path contracts."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.main import get_command
from typer.testing import CliRunner

import foresight_ocr.cli.main as cli_main
from foresight_ocr import __version__
from foresight_ocr.cli.main import app
from foresight_ocr.diagnostics import core_diagnostics
from foresight_ocr.ocr import runners
from foresight_ocr.ocr.runners import _runner, _venv_python


def test_version_option_reports_package_version() -> None:
    result = CliRunner().invoke(app, ["--version"])

    assert result.exit_code == 0
    assert result.stdout.strip() == __version__


def test_every_registered_command_renders_help() -> None:
    command = get_command(app)
    runner = CliRunner()
    failures: list[str] = []

    for name in sorted(command.commands):
        result = runner.invoke(app, [name, "--help"])
        if result.exit_code:
            failures.append(f"{name}: {result.exception!r}\n{result.output}")

    assert not failures, "\n\n".join(failures)


def test_cli_entrypoint_configures_utf8_streams(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Stream:
        encoding: str | None = None

        def reconfigure(self, *, encoding: str) -> None:
            self.encoding = encoding

    stdout = Stream()
    stderr = Stream()
    called: list[bool] = []
    monkeypatch.setattr(cli_main.sys, "stdout", stdout)
    monkeypatch.setattr(cli_main.sys, "stderr", stderr)
    monkeypatch.setattr(cli_main, "app", lambda: called.append(True))

    cli_main.main()

    assert stdout.encoding == "utf-8"
    assert stderr.encoding == "utf-8"
    assert called == [True]


def test_backend_interpreter_path_is_platform_specific(tmp_path: Path) -> None:
    assert _venv_python(tmp_path, ".venv-paddle", platform="posix") == (
        tmp_path / ".venv-paddle" / "bin" / "python"
    )
    assert _venv_python(tmp_path, ".venv-paddle", platform="nt") == (
        tmp_path / ".venv-paddle" / "Scripts" / "python.exe"
    )


def test_runner_prefers_packaged_copy_then_falls_back_to_checkout(
    tmp_path: Path,
) -> None:
    runner = _runner(tmp_path, "ppocr_v5.py")

    bundled = Path(runners.__file__).resolve().parents[1]
    bundled = bundled / "backend_runners" / "ppocr_v5.py"
    checkout = Path(runners.__file__).resolve().parents[3]
    checkout = checkout / "runners" / "ppocr_v5.py"
    expected = bundled if bundled.is_file() else checkout
    assert runner == expected


def test_core_installation_diagnostics_from_unrelated_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    failures = [check for check in core_diagnostics() if not check.ok]

    assert failures == []

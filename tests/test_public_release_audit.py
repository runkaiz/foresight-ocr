import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from audit_public_release import _content_findings, _path_findings


def test_public_audit_rejects_private_paths_and_payloads(tmp_path: Path) -> None:
    secret = tmp_path / "notes.md"
    secret.write_text(
        "/" + "Users/example/private/file\n-----BEGIN " + "OPENSSH PRIVATE KEY-----\n",
        encoding="utf-8",
    )

    findings = _content_findings(tmp_path, "notes.md", 10_000)
    messages = {finding.message for finding in findings}
    assert "contains a machine-specific absolute path" in messages
    assert "contains a possible private key" in messages
    assert _path_findings("source/book.pdf", scope="worktree")


def test_public_audit_detects_extension_mismatch(tmp_path: Path) -> None:
    image = tmp_path / "screenshot.png"
    image.write_bytes(b"\xff\xd8\xffnot-really-a-png")

    findings = _content_findings(tmp_path, "screenshot.png", 10_000)
    assert [finding.message for finding in findings] == [
        "extension is .png but content is not PNG"
    ]

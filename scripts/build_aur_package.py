#!/usr/bin/env python3
"""Generate an AUR-ready foresight-ocr-bin repository from release archives."""

from __future__ import annotations

import argparse
import hashlib
import io
import re
import tarfile
import tomllib
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
TARGETS = ("linux-x86_64", "linux-arm64")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _version() -> str:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    version = str(project["project"]["version"])
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]+){2}(?:[a-z0-9.]*)?", version):
        raise SystemExit(f"version is not valid for the AUR recipe: {version}")
    return version


def _verify_archive(path: Path, version: str, target: str) -> None:
    expected_name = f"foresight-ocr-{version}-{target}.tar.gz"
    if path.name != expected_name:
        raise SystemExit(f"expected {expected_name}, received {path.name}")
    expected_root = expected_name.removesuffix(".tar.gz")
    required = {
        PurePosixPath(expected_root, "foresight-ocr"),
        PurePosixPath(expected_root, "LICENSE"),
        PurePosixPath(expected_root, "THIRD_PARTY_NOTICES.txt"),
    }
    names: set[PurePosixPath] = set()
    with tarfile.open(path, "r:gz") as archive:
        for member in archive.getmembers():
            name = PurePosixPath(member.name)
            if name.is_absolute() or ".." in name.parts:
                raise SystemExit(f"unsafe release archive member: {member.name}")
            if not name.parts or name.parts[0] != expected_root:
                raise SystemExit(f"release archive member escaped root: {member.name}")
            if member.issym() or member.islnk():
                raise SystemExit(f"release archive contains a link: {member.name}")
            if member.isfile():
                names.add(name)
    missing = sorted(str(name) for name in required - names)
    if missing:
        raise SystemExit(f"release archive is incomplete: {missing}")


def _source(version: str, target: str, *, dynamic_tag: bool) -> str:
    filename = f"foresight-ocr-{version}-{target}.tar.gz"
    tag_version = "${pkgver}" if dynamic_tag else version
    return (
        f"{filename}::https://github.com/runkaiz/foresight-ocr/releases/download/"
        f"v{tag_version}/{filename}"
    )


def _pkgbuild(version: str, checksums: dict[str, str]) -> str:
    return f"""# Maintainer: Runkai Zhang <runkaiz at users dot noreply dot github dot com>
pkgname=foresight-ocr-bin
pkgver={version}
pkgrel=1
pkgdesc='Provenance-first OCR for Chinese genealogy records (prebuilt)'
arch=('x86_64' 'aarch64')
url='https://github.com/runkaiz/foresight-ocr'
license=('Apache-2.0')
depends=('glibc>=2.35')
provides=('foresight-ocr')
conflicts=('foresight-ocr')
options=('!strip')
source_x86_64=("{_source(version, "linux-x86_64", dynamic_tag=True)}")
source_aarch64=("{_source(version, "linux-arm64", dynamic_tag=True)}")
sha256sums_x86_64=('{checksums["linux-x86_64"]}')
sha256sums_aarch64=('{checksums["linux-arm64"]}')

package() {{
  local target
  case "$CARCH" in
    x86_64) target=linux-x86_64 ;;
    aarch64) target=linux-arm64 ;;
    *) return 1 ;;
  esac

  install -d "$pkgdir/opt/foresight-ocr" "$pkgdir/usr/bin"
  cp -a "$srcdir/foresight-ocr-$pkgver-$target/." \
    "$pkgdir/opt/foresight-ocr/"
  ln -s /opt/foresight-ocr/foresight-ocr "$pkgdir/usr/bin/foresight-ocr"
}}
"""


def _srcinfo(version: str, checksums: dict[str, str]) -> str:
    lines = [
        "pkgbase = foresight-ocr-bin",
        "\tpkgdesc = Provenance-first OCR for Chinese genealogy records (prebuilt)",
        f"\tpkgver = {version}",
        "\tpkgrel = 1",
        "\turl = https://github.com/runkaiz/foresight-ocr",
        "\tarch = x86_64",
        "\tarch = aarch64",
        "\tlicense = Apache-2.0",
        "\tdepends = glibc>=2.35",
        "\tprovides = foresight-ocr",
        "\tconflicts = foresight-ocr",
        "\toptions = !strip",
        "\tsource_x86_64 = " + _source(version, "linux-x86_64", dynamic_tag=False),
        f"\tsha256sums_x86_64 = {checksums['linux-x86_64']}",
        "\tsource_aarch64 = " + _source(version, "linux-arm64", dynamic_tag=False),
        f"\tsha256sums_aarch64 = {checksums['linux-arm64']}",
        "",
        "pkgname = foresight-ocr-bin",
        "",
    ]
    return "\n".join(lines)


def _aur_license() -> str:
    return """Zero-Clause BSD

Permission to use, copy, modify, and/or distribute this software for any
purpose with or without fee is hereby granted.

THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES WITH
REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF MERCHANTABILITY
AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR ANY SPECIAL, DIRECT,
INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES WHATSOEVER RESULTING FROM
LOSS OF USE, DATA OR PROFITS, WHETHER IN AN ACTION OF CONTRACT, NEGLIGENCE OR
OTHER TORTIOUS ACTION, ARISING OUT OF OR IN CONNECTION WITH THE USE OR
PERFORMANCE OF THIS SOFTWARE.
"""


def _add_text(archive: tarfile.TarFile, root: str, name: str, content: str) -> None:
    payload = content.encode("utf-8")
    info = tarfile.TarInfo(f"{root}/{name}")
    info.mode = 0o644
    info.mtime = 0
    info.size = len(payload)
    archive.addfile(info, io.BytesIO(payload))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("x86_64_archive", type=Path)
    parser.add_argument("arm64_archive", type=Path)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist")
    args = parser.parse_args()

    version = _version()
    archives = {
        "linux-x86_64": args.x86_64_archive.resolve(),
        "linux-arm64": args.arm64_archive.resolve(),
    }
    checksums: dict[str, str] = {}
    for target, path in archives.items():
        _verify_archive(path, version, target)
        checksums[target] = _sha256(path)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    root = f"foresight-ocr-bin-{version}"
    output = args.output_dir / f"{root}-1-aur.tar.gz"
    with tarfile.open(output, "w:gz") as archive:
        _add_text(archive, root, "PKGBUILD", _pkgbuild(version, checksums))
        _add_text(archive, root, ".SRCINFO", _srcinfo(version, checksums))
        _add_text(archive, root, "LICENSE", _aur_license())
        _add_text(
            archive,
            root,
            ".gitignore",
            "*\n!.gitignore\n!PKGBUILD\n!.SRCINFO\n!LICENSE\n",
        )

    with tarfile.open(output, "r:gz") as archive:
        names = {member.name for member in archive.getmembers() if member.isfile()}
        expected = {f"{root}/{name}" for name in ("PKGBUILD", ".SRCINFO", "LICENSE")}
        if not expected.issubset(names):
            raise SystemExit(f"AUR bundle is incomplete: {sorted(expected - names)}")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

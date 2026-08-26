"""foresight-ocr CLI.

Each subcommand is one independently rerunnable pipeline stage. Changing the OCR
model must never force a re-extract or a re-layout, so stages communicate only
through artifacts on disk and rows in SQLite.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from datetime import datetime, timezone
from functools import wraps
from math import isfinite
from pathlib import Path
from typing import Any, Callable, Optional, ParamSpec, TypeVar, cast

import typer
from rich.console import Console
from rich.table import Table

from foresight_ocr import __version__
from foresight_ocr.document import extract_originals, inspect_pdf
from foresight_ocr.imaging.io import read_image, write_image
from foresight_ocr.persistence import connect, init_schema
from foresight_ocr.project import InvalidDocumentId, Project, validate_document_id
from foresight_ocr.provenance import ProcessingRun


def _version(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit()


app = typer.Typer(add_completion=False, help="Chinese genealogy OCR pipeline.")
console = Console()


def _configure_utf8_stdio() -> None:
    """Keep Unicode CLI output usable under legacy Windows code pages."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")


def main() -> None:
    """Run the CLI after normalizing its text streams."""
    _configure_utf8_stdio()
    app()


@app.callback()
def _main(
    version: bool = typer.Option(
        False,
        "--version",
        callback=_version,
        is_eager=True,
        help="Show the installed Foresight OCR version and exit.",
    ),
) -> None:
    """Chinese genealogy OCR pipeline."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _open_db(project: Project):
    conn = connect(project.db_path)
    init_schema(conn)
    return conn


def _activate(project: Project, document_id: str):
    """Load this document's profile before anything reads band labels."""
    from foresight_ocr.context import set_profile
    from foresight_ocr.document.profile import load_profile

    try:
        document_id = validate_document_id(document_id)
    except InvalidDocumentId as exc:
        raise typer.BadParameter(str(exc)) from exc
    profile = load_profile(project.configs, document_id)
    set_profile(profile)
    return profile


@app.command("backends")
def backend_status() -> None:
    """Check whether each optional OCR backend environment is usable."""
    from foresight_ocr.ocr.base import get_backend

    table = Table(title="OCR backends")
    table.add_column("backend")
    table.add_column("status")
    table.add_column("detail")
    for name in ("ppocr_v5", "paddleocr_vl"):
        backend = get_backend(name)
        available, detail = backend.available()
        status = (
            "[green]available[/green]" if available else "[yellow]unavailable[/yellow]"
        )
        table.add_row(name, status, detail)
    console.print(table)


@app.command()
def doctor() -> None:
    """Exercise core native libraries and packaged resources."""
    from foresight_ocr.diagnostics import core_diagnostics

    diagnostics = core_diagnostics()
    table = Table(title="Foresight OCR installation")
    table.add_column("check")
    table.add_column("status")
    table.add_column("detail")
    for check in diagnostics:
        status = "[green]ok[/green]" if check.ok else "[red]failed[/red]"
        table.add_row(check.name, status, check.detail)
    console.print(table)
    if not all(check.ok for check in diagnostics):
        raise typer.Exit(1)


# Pipeline locks, held from _start_run to _finish_run. Every mutating stage
# brackets itself with that pair already, so this is where "one writer per
# document" belongs; if the process dies in between, the kernel drops the lock.
_RUN_LOCKS: dict[int, tuple[Any, Any]] = {}
_P = ParamSpec("_P")
_R = TypeVar("_R")


def _artifacts_of(conn) -> Path:
    """The artifacts directory, from the connection's own database file."""
    for _, name, path in conn.execute("PRAGMA database_list"):
        if name == "main" and path:
            return Path(path).parent
    raise RuntimeError("database is not file-backed")


def _start_run(conn, document_id: str, run: ProcessingRun) -> int:
    from foresight_ocr.persistence.locks import StageBusy, stage_lock

    guard = stage_lock(_artifacts_of(conn), document_id, run.stage)
    try:
        guard.__enter__()
    except StageBusy as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc

    row = run.as_row()
    cur = conn.execute(
        """INSERT INTO processing_runs
           (document_id, stage, params_json, params_hash, input_checksum,
            compute_backend, pipeline_version, git_commit, started_at, status)
           VALUES (?,?,?,?,?,?,?,?,?, 'running')""",
        (
            document_id,
            row["stage"],
            row["params_json"],
            row["params_hash"],
            row["input_checksum"],
            row["compute_backend"],
            row["pipeline_version"],
            row["git_commit"],
            row["started_at"],
        ),
    )
    conn.commit()
    run_id = int(cur.lastrowid)
    _RUN_LOCKS[run_id] = (conn, guard)
    return run_id


def _finish_run(conn, run_id: int, status: str = "completed") -> None:
    active = _RUN_LOCKS.pop(run_id, None)
    try:
        conn.execute(
            "UPDATE processing_runs SET finished_at = ?, status = ? WHERE id = ?",
            (_now(), status, run_id),
        )
        conn.commit()
    finally:
        if active is not None:
            _, guard = active
            guard.__exit__(None, None, None)


def _fail_active_runs() -> None:
    """Record command failures and release every stage lock held by this process."""
    for run_id, (conn, guard) in list(_RUN_LOCKS.items()):
        try:
            conn.execute(
                "UPDATE processing_runs SET finished_at = ?, status = 'failed' "
                "WHERE id = ?",
                (_now(), run_id),
            )
            conn.commit()
        finally:
            _RUN_LOCKS.pop(run_id, None)
            guard.__exit__(None, None, None)


def _records_run_failure(command: Callable[_P, _R]) -> Callable[_P, _R]:
    """Make a pipeline command's run record and advisory lock exception-safe."""

    @wraps(command)
    def guarded(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        try:
            return command(*args, **kwargs)
        except BaseException:
            _fail_active_runs()
            raise

    return guarded


@app.command()
@_records_run_failure
def inspect(
    pdf: Path = typer.Argument(..., exists=True, dir_okay=False),
    document_id: Optional[str] = typer.Option(None, "--id"),
    report: bool = typer.Option(True, help="Write docs/corpus-analysis.md"),
) -> None:
    """Read PDF structure and record the corpus baseline."""
    project = Project.discover()
    try:
        info = inspect_pdf(pdf, document_id)
    except InvalidDocumentId as exc:
        raise typer.BadParameter(str(exc)) from exc
    conn = _open_db(project)

    # The document row must exist before any run can reference it.
    conn.execute(
        """INSERT INTO documents (id, title, source_path, checksum, page_count, created_at)
           VALUES (?,?,?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET
             source_path=excluded.source_path,
             checksum=excluded.checksum,
             page_count=excluded.page_count""",
        (info.document_id, pdf.stem, str(pdf), info.checksum, info.page_count, _now()),
    )
    conn.commit()

    run = ProcessingRun(
        stage="inspect",
        params={"pdf": str(pdf), "document_id": info.document_id},
        input_checksum=info.checksum,
    )
    run_id = _start_run(conn, info.document_id, run)

    conn.executemany(
        """INSERT INTO pages
           (document_id, page_index, width, height, x_ppi, y_ppi, colorspace, encoding)
           VALUES (?,?,?,?,?,?,?,?)
           ON CONFLICT(document_id, page_index) DO UPDATE SET
             width=excluded.width, height=excluded.height,
             x_ppi=excluded.x_ppi, y_ppi=excluded.y_ppi,
             colorspace=excluded.colorspace, encoding=excluded.encoding""",
        [
            (
                info.document_id,
                p.page_index,
                p.width,
                p.height,
                p.x_ppi,
                p.y_ppi,
                p.colorspace,
                p.encoding,
            )
            for p in info.pages
        ],
    )
    conn.commit()
    _finish_run(conn, run_id)

    out = project.analysis_dir(info.document_id, "inspect")
    out.mkdir(parents=True, exist_ok=True)
    (out / "structure.json").write_text(info.to_json(), encoding="utf-8")

    table = Table(title=f"{info.document_id} — corpus structure")
    table.add_column("property")
    table.add_column("value")
    table.add_row("pages", str(info.page_count))
    table.add_row("pdf version", info.pdf_version)
    table.add_row("producer", info.producer or "-")
    table.add_row("sha256", info.checksum[:16] + "…")
    for name, counts in info.uniformity().items():
        rendered = ", ".join(f"{v} ×{n}" for v, n in counts[:4])
        if len(counts) > 4:
            rendered += f", +{len(counts) - 4} more"
        table.add_row(name, rendered)
    console.print(table)

    from foresight_ocr.document.profile import load_profile, save_profile

    profile = load_profile(project.configs, info.document_id)
    saved = save_profile(project.configs, profile)
    console.print(
        f"[green]profile[/green] {saved.name}: bands "
        f"{'/'.join(profile.band_labels) or '—'} "
        f"({profile.bands_per_page}), chain {'→'.join(profile.generation_chain)}"
    )

    if report:
        from foresight_ocr.document.report import write_corpus_analysis

        path = write_corpus_analysis(project, info)
        console.print(f"[green]wrote[/green] {path}")


@app.command()
@_records_run_failure
def extract(
    document_id: str = typer.Argument(...),
    pages: Optional[str] = typer.Option(None, help="e.g. 1,2,10-20"),
    force: bool = typer.Option(False, help="Overwrite existing originals"),
) -> None:
    """Preserve the embedded original scans and decode PNG derivatives."""
    project = Project.discover()
    conn = _open_db(project)
    _activate(project, document_id)
    doc = conn.execute(
        "SELECT * FROM documents WHERE id = ?", (document_id,)
    ).fetchone()
    if doc is None:
        raise typer.BadParameter(f"unknown document {document_id!r}; run inspect first")

    selected = _parse_pages(pages)
    run = ProcessingRun(
        stage="extract",
        params={"pages": pages or "all", "force": force},
        input_checksum=doc["checksum"],
    )
    run_id = _start_run(conn, document_id, run)

    raw_dir = project.pages_dir(document_id, "original")
    decoded_dir = project.pages_dir(document_id, "decoded")
    with console.status("extracting embedded rasters…"):
        results = extract_originals(
            Path(doc["source_path"]), raw_dir, decoded_dir, selected, force
        )

    rows = []
    for r in results:
        rows.append(
            (
                document_id,
                r.page_index,
                "original",
                str(r.original_path),
                r.original_checksum,
                run_id,
                _now(),
            )
        )
        rows.append(
            (
                document_id,
                r.page_index,
                "decoded",
                str(r.decoded_path),
                r.decoded_checksum,
                run_id,
                _now(),
            )
        )
    conn.executemany(
        """INSERT INTO page_assets
           (document_id, page_index, role, path, checksum, run_id, created_at)
           VALUES (?,?,?,?,?,?,?)
           ON CONFLICT(document_id, page_index, role) DO UPDATE SET
             path=excluded.path, checksum=excluded.checksum, run_id=excluded.run_id""",
        rows,
    )
    conn.commit()
    _finish_run(conn, run_id)
    console.print(
        f"[green]extracted[/green] {len(results)} pages → {raw_dir} (+ decoded PNGs)"
    )


def _parse_pages(spec: str | None) -> list[int] | None:
    if not spec:
        return None
    out: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            out.extend(range(int(a), int(b) + 1))
        elif part:
            out.append(int(part))
    return sorted(set(out))


@app.command()
@_records_run_failure
def normalize(
    document_id: str = typer.Argument(...),
    pages: Optional[str] = typer.Option(None, help="e.g. 1,2,10-20"),
    downscale: int = typer.Option(4, help="Frame detection works at 1/N scale"),
    write_pages: bool = typer.Option(True, help="Write warped canonical PNGs"),
    use_fallback: bool = typer.Option(
        True,
        "--fallback/--no-fallback",
        help="Place the corpus median frame on pages that cannot be fitted",
    ),
) -> None:
    """Detect the printed page frame and warp pages into canonical space."""
    import numpy as np

    from foresight_ocr.imaging.overlay import contact_sheet, draw_frame_overlay
    from foresight_ocr.layout.frame import FrameFit, detect_frame, refit_with_prior
    from foresight_ocr.layout.normalize import (
        FramePass,
        build_canonical_space,
        median_frame,
        normalize_page,
        roundtrip_error,
        warp_page,
    )

    project = Project.discover()
    conn = _open_db(project)
    _activate(project, document_id)
    doc = conn.execute(
        "SELECT * FROM documents WHERE id = ?", (document_id,)
    ).fetchone()
    if doc is None:
        raise typer.BadParameter(f"unknown document {document_id!r}")

    selected = _parse_pages(pages)
    assets = conn.execute(
        "SELECT page_index, path FROM page_assets "
        "WHERE document_id = ? AND role = 'decoded' ORDER BY page_index",
        (document_id,),
    ).fetchall()
    if not assets:
        raise typer.BadParameter("no decoded pages; run extract first")
    targets = [a for a in assets if not selected or a["page_index"] in selected]

    run = ProcessingRun(
        stage="normalize",
        params={"downscale": downscale, "pages": pages or "all"},
        input_checksum=doc["checksum"],
    )
    run_id = _start_run(conn, document_id, run)

    # Pass 1 — fit a frame on every page from the outermost detected rules.
    passes: list = []
    shapes: dict[int, tuple[int, int]] = {}
    with console.status(f"pass 1/2: fitting frames on {len(targets)} pages…") as status:
        for i, a in enumerate(targets, 1):
            img = _imread(Path(a["path"]))
            shapes[a["page_index"]] = img.shape[:2]
            fit = detect_frame(img, downscale=downscale)
            passes.append(FramePass(a["page_index"], fit, Path(a["path"])))
            status.update(f"pass 1/2: frames {i}/{len(targets)}")

    space = build_canonical_space(passes)
    console.print(
        f"first-pass canonical space [bold]{space.width}×{space.height}[/bold] px "
        f"(MAD {space.width_mad:.1f}×{space.height_mad:.1f})"
    )

    # Re-select borders against the corpus median, then rebuild the space from
    # the corrected fits. Pages whose border was faded or cropped away stop
    # dragging an interior rule into the frame.
    for p in passes:
        refit = refit_with_prior(
            shapes[p.page_index], p.fit, space.median_width, space.median_height
        )
        # Take the refit whenever it produced a frame at all. Keeping the
        # first-pass fit as a fallback would preserve exactly the silently-wrong
        # narrow frames this pass exists to catch.
        if refit.corners:
            p.fit = refit
        elif not refit.corners and refit.reason:
            p.fit.ok = False
            p.fit.reason = refit.reason
    space = build_canonical_space(passes)
    console.print(
        f"refit canonical space [bold]{space.width}×{space.height}[/bold] px "
        f"(MAD {space.width_mad:.1f}×{space.height_mad:.1f})"
    )

    # Last resort for pages the detector cannot fit at all: place the corpus
    # median frame. A guessed frame that the reviewer can see and reject beats
    # dropping the page's entries silently — those entries are real people.
    fallback = median_frame(passes) if use_fallback else None
    fallback_pages: list[int] = []
    if fallback:
        for p in passes:
            if p.fit.corners:
                continue
            p.fit.corners = [list(c) for c in fallback]
            p.fit.ok = True
            p.fit.inferred_edges = ["frame-from-corpus-median"]
            p.fit.reason = "frame taken from corpus median; verify visually"
            fallback_pages.append(p.page_index)
        if fallback_pages:
            console.print(
                f"[cyan]{len(fallback_pages)}[/cyan] page(s) using the corpus "
                f"median frame: {fallback_pages}"
            )

    # Pass 2 — grade each fit against the corpus median and warp.
    norm_dir = project.pages_dir(document_id, "normalized")
    overlay_dir = project.analysis_dir(document_id, "frames")
    norm_dir.mkdir(parents=True, exist_ok=True)

    # Canonical size is derived from the corpus, so it can move between runs.
    # Pages left over at the old size would silently mix two coordinate systems
    # in one directory, so clear them out when the space changes.
    space_file = norm_dir / "canonical_space.json"
    current = json.dumps({"width": space.width, "height": space.height})
    if space_file.exists() and space_file.read_text() != current:
        for stale in norm_dir.glob("p*.png"):
            stale.unlink()
        console.print("[yellow]canonical space changed[/yellow] — cleared stale pages")
    space_file.write_text(current)
    results = []
    worst_roundtrip = 0.0
    with console.status("pass 2/2: warping…") as status:
        for i, p in enumerate(passes, 1):
            norm = normalize_page(p.fit, space, p.page_index)
            if not norm.ok and fallback:
                # The detected frame is implausible. Substitute the corpus
                # median rather than dropping the page: its entries are real
                # people, and a flagged guess the reviewer can see and reject is
                # better than a silent omission.
                guess = FrameFit(
                    corners=[list(c) for c in fallback],
                    skew_deg=0.0,
                    width=space.median_width,
                    height=space.median_height,
                    rect_error=0.0,
                    line_residual=float("nan"),
                    interior_h=[],
                    detected_edges=0,
                    ok=True,
                    reason="frame taken from corpus median; verify visually",
                    inferred_edges=["frame-from-corpus-median"],
                )
                norm = normalize_page(guess, space, p.page_index)
                norm.status = "fallback"
                norm.reason = "frame taken from corpus median; verify visually"
                if p.page_index not in fallback_pages:
                    fallback_pages.append(p.page_index)
            results.append(norm)
            img = _imread(p.path)

            if norm.forward:
                probe = np.array(
                    [
                        [0, 0],
                        [img.shape[1], 0],
                        [img.shape[1], img.shape[0]],
                        [0, img.shape[0]],
                        [img.shape[1] / 2, img.shape[0] / 2],
                    ],
                    dtype=np.float64,
                )
                worst_roundtrip = max(worst_roundtrip, roundtrip_error(norm, probe))

                tid = f"{document_id}:{p.page_index}:frame:{run.params_hash}"
                conn.execute(
                    """INSERT INTO transforms
                       (id, document_id, page_index, kind, forward_json, inverse_json,
                        residual, status, run_id)
                       VALUES (?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(id) DO UPDATE SET
                         forward_json=excluded.forward_json,
                         inverse_json=excluded.inverse_json,
                         residual=excluded.residual, status=excluded.status""",
                    (
                        tid,
                        document_id,
                        p.page_index,
                        "frame_homography",
                        json.dumps(norm.forward),
                        json.dumps(norm.inverse),
                        p.fit.line_residual,
                        "automatic" if norm.ok else "needs_review",
                        run_id,
                    ),
                )
                if write_pages and norm.ok:
                    out = norm_dir / f"p{p.page_index:04d}.png"
                    write_image(out, warp_page(img, norm.forward, space))

            caption = (
                f"p{p.page_index}  {'OK' if norm.ok else 'FLAG'}\n"
                f"skew {norm.skew_deg:+.2f}deg  {norm.width:.0f}x{norm.height:.0f}\n"
                f"{norm.reason[:48]}"
            )
            draw_frame_overlay(
                img,
                p.fit.corners,
                p.fit.interior_h,
                overlay_dir / f"p{p.page_index:04d}.png",
                norm.ok,
                caption,
            )
            status.update(f"pass 2/2: warping {i}/{len(passes)}")

    conn.commit()
    _finish_run(conn, run_id)

    summary = {
        "canonical_space": space.to_dict(),
        "worst_roundtrip_px": worst_roundtrip,
        "pages": [r.to_dict() for r in results],
    }
    out_json = project.analysis_dir(document_id, "frames") / "summary.json"
    out_json.write_text(json.dumps(summary, ensure_ascii=False, indent=2), "utf-8")

    clean = [r for r in results if r.status == "clean"]
    fell_back = [r for r in results if r.status == "fallback"]
    inferred = [r for r in results if r.status == "inferred"]
    flagged = [r for r in results if r.status == "failed"]
    skews = sorted(abs(r.skew_deg) for r in results if r.forward)
    console.print(
        f"[green]{len(clean)}[/green] clean, "
        f"[cyan]{len(inferred)}[/cyan] with an inferred edge, "
        f"[yellow]{len(flagged)}[/yellow] failed, "
        f"[magenta]{len(fell_back)}[/magenta] on the median frame; "
        f"worst coordinate round-trip {worst_roundtrip:.4f} px"
    )
    if skews:
        console.print(
            f"skew: median {skews[len(skews) // 2]:.2f}°, "
            f"p95 {skews[int(len(skews) * 0.95)]:.2f}°, max {skews[-1]:.2f}°"
        )
    if flagged:
        table = Table(title="flagged pages")
        table.add_column("page")
        table.add_column("reason")
        for r in flagged[:25]:
            table.add_row(str(r.page_index), r.reason)
        console.print(table)

    sheet_pages = [
        overlay_dir / f"p{r.page_index:04d}.png"
        for r in results[:: max(1, len(results) // 20)]
    ]
    sheet = contact_sheet(sheet_pages, overlay_dir / "contact_sheet.png")
    console.print(f"[green]overlays[/green] {overlay_dir}  (sheet: {sheet.name})")


@app.command()
@_records_run_failure
def restore(
    document_id: str = typer.Argument(...),
    sample: int = typer.Option(12, min=1, help="How many pages to evaluate"),
    write_variants: bool = typer.Option(
        False, help="Also write full-page variants for every sampled page"
    ),
) -> None:
    """Benchmark watermark-suppression variants and write the comparison."""
    import numpy as np

    from foresight_ocr.imaging.variants import VARIANTS, build_variant
    from foresight_ocr.imaging.watermark_eval import (
        build_pixel_sets,
        comparison_strip,
        score_variant,
        watermark_bbox,
    )

    project = Project.discover()
    conn = _open_db(project)
    _activate(project, document_id)
    doc = conn.execute(
        "SELECT * FROM documents WHERE id = ?", (document_id,)
    ).fetchone()
    if doc is None:
        raise typer.BadParameter(f"unknown document {document_id!r}")

    assets = conn.execute(
        "SELECT pa.page_index, pa.path FROM page_assets pa "
        "JOIN pages p ON p.document_id = pa.document_id "
        "AND p.page_index = pa.page_index "
        "WHERE pa.document_id = ? AND pa.role = 'decoded' AND p.ignored = 0 "
        "ORDER BY pa.page_index",
        (document_id,),
    ).fetchall()
    if not assets:
        raise typer.BadParameter("no decoded, non-ignored pages; run extract first")

    # Spread the sample across the book rather than taking the first N pages:
    # paper tone and stamp intensity drift from front to back.
    step = max(1, len(assets) // sample)
    picked = assets[::step][:sample]

    run = ProcessingRun(
        stage="restore",
        params={"sample": sample, "variants": sorted(VARIANTS)},
        input_checksum=doc["checksum"],
    )
    run_id = _start_run(conn, document_id, run)

    out_dir = project.analysis_dir(document_id, "watermark")
    out_dir.mkdir(parents=True, exist_ok=True)
    per_variant: dict[str, list] = {name: [] for name in VARIANTS}

    with console.status("scoring watermark variants…") as status:
        for i, a in enumerate(picked, 1):
            bgr = _imread(Path(a["path"]))
            sets = build_pixel_sets(bgr)
            box = watermark_bbox(bgr)
            crops = []
            for name in VARIANTS:
                var = build_variant(bgr, name)
                per_variant[name].append(score_variant(var, sets, name))
                if box:
                    x, y, w, h = box
                    crops.append((name, var[y : y + h, x : x + w]))
                if write_variants:
                    vdir = project.pages_dir(document_id, f"variant_{name}")
                    vdir.mkdir(parents=True, exist_ok=True)
                    write_image(vdir / f"p{a['page_index']:04d}.png", var)
            if box:
                x, y, w, h = box
                crops.insert(0, ("original", bgr[y : y + h, x : x + w]))
                write_image(
                    out_dir / f"compare_p{a['page_index']:04d}.png",
                    comparison_strip(crops),
                )
            status.update(f"scoring {i}/{len(picked)}")

    conn.commit()
    _finish_run(conn, run_id)

    table = Table(title=f"watermark suppression — mean over {len(picked)} pages")
    table.add_column("variant")
    table.add_column("wm residual ↓", justify="right")
    table.add_column("ink contrast under stamp ↑", justify="right")
    table.add_column("ink contrast clean", justify="right")
    table.add_column("ink retention ↑", justify="right")

    summary = {}

    def finite_mean(values: list[float]) -> float | None:
        finite = [value for value in values if np.isfinite(value)]
        return float(np.mean(finite)) if finite else None

    def render(value: float | None, digits: int) -> str:
        return "—" if value is None else f"{value:.{digits}f}"

    for name, scores in per_variant.items():
        watermark_residual = finite_mean([s.watermark_residual for s in scores])
        ink_contrast_under = finite_mean([s.ink_contrast_under for s in scores])
        ink_contrast_clean = finite_mean([s.ink_contrast_clean for s in scores])
        ink_retention = finite_mean([s.ink_retention for s in scores])
        agg = {
            "watermark_residual": watermark_residual,
            "ink_contrast_under": ink_contrast_under,
            "ink_contrast_clean": ink_contrast_clean,
            "ink_retention": ink_retention,
            "description": VARIANTS[name],
        }
        summary[name] = agg
        table.add_row(
            name,
            render(watermark_residual, 1),
            render(ink_contrast_under, 1),
            render(ink_contrast_clean, 1),
            render(ink_retention, 3),
        )
    console.print(table)

    (out_dir / "scores.json").write_text(
        json.dumps(
            {"pages": [a["page_index"] for a in picked], "variants": summary},
            ensure_ascii=False,
            indent=2,
            allow_nan=False,
        ),
        encoding="utf-8",
    )
    console.print(f"[green]comparisons[/green] {out_dir}")


@app.command()
@_records_run_failure
def layout(
    document_id: str = typer.Argument(...),
    bands: int = typer.Option(
        0, help="Expected bands per page; 0 uses the document profile"
    ),
    overlays: int = typer.Option(24, help="How many overlay pages to render"),
) -> None:
    """Recover band and column geometry, then learn the document template."""
    import cv2
    import yaml

    from foresight_ocr.imaging.overlay import contact_sheet, draw_grid_overlay
    from foresight_ocr.layout.template import (
        analyse_page,
        assign_layout_families,
        build_template,
        repair_bands_from_template,
    )

    project = Project.discover()
    conn = _open_db(project)
    _activate(project, document_id)
    doc = conn.execute(
        "SELECT * FROM documents WHERE id = ?", (document_id,)
    ).fetchone()
    if doc is None:
        raise typer.BadParameter(f"unknown document {document_id!r}")

    profile = _activate(project, document_id)
    if not bands:
        bands = profile.bands_per_page or 3
    norm_dir = project.pages_dir(document_id, "normalized")
    page_files = sorted(norm_dir.glob("p*.png"))
    if not page_files:
        raise typer.BadParameter("no normalized pages; run normalize first")

    run = ProcessingRun(
        stage="layout",
        params={"bands": bands},
        input_checksum=doc["checksum"],
    )
    run_id = _start_run(conn, document_id, run)

    structures = []
    with console.status(f"analysing {len(page_files)} normalized pages…") as status:
        for i, f in enumerate(page_files, 1):
            gray = _imread(f, cv2.IMREAD_GRAYSCALE)
            page_index = int(f.stem[1:])
            structures.append(analyse_page(gray, page_index, expected_bands=bands))
            status.update(f"analysing {i}/{len(page_files)}")

    h, w = _imread(page_files[0], cv2.IMREAD_GRAYSCALE).shape[:2]
    # First learn the modal corpus structure, then use it as an independent vote
    # on pages where one horizontal rule disappeared or a crease looked like an
    # extra one. Covers and larger disagreements remain untouched as outliers.
    template = build_template(structures, w, h)
    repaired_pages = [
        s.page_index for s in structures if repair_bands_from_template(s, template)
    ]
    if repaired_pages:
        template = build_template(structures, w, h)

    if template.band_count < 1 or len(template.band_edges) != template.band_count + 1:
        raise RuntimeError(
            "layout could not learn a complete band structure; inspect the "
            "normalized pages and rerun with --bands"
        )
    if not isfinite(template.column_pitch) or template.column_pitch <= 0:
        raise RuntimeError(
            "layout found no measurable entry-column pitch; inspect the normalized "
            "pages before segmenting"
        )
    template.layout_families = assign_layout_families(structures, template)

    console.print(
        f"template: [bold]{template.band_count}[/bold] bands, "
        f"edges {[round(e) for e in template.band_edges]} "
        f"(MAD {[round(m, 1) for m in template.band_edge_mad]}), "
        f"column pitch [bold]{template.column_pitch:.1f}[/bold] px "
        f"(MAD {template.column_pitch_mad:.1f})"
    )
    if repaired_pages:
        preview = ", ".join(str(page) for page in repaired_pages[:20])
        suffix = "…" if len(repaired_pages) > 20 else ""
        console.print(
            f"band repair: [green]{len(repaired_pages)}[/green] page(s) aligned "
            f"to the corpus template ({preview}{suffix})"
        )
    fam_table = Table(title="layout families")
    fam_table.add_column("family")
    fam_table.add_column("pages", justify="right")
    fam_table.add_column("examples")
    for name, pages_in in sorted(template.layout_families.items()):
        fam_table.add_row(name, str(len(pages_in)), str(pages_in[:10])[:60])
    console.print(fam_table)

    # Persist per-page geometry so segmentation can consume it.
    for s in structures:
        cur = conn.execute(
            """INSERT INTO page_layouts
               (document_id, page_index, layout_family, frame_json, template_fit,
                is_outlier, run_id)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(document_id, page_index, run_id) DO UPDATE SET
                 layout_family=excluded.layout_family,
                 frame_json=excluded.frame_json""",
            (
                document_id,
                s.page_index,
                next(
                    (
                        k
                        for k, v in template.layout_families.items()
                        if s.page_index in v
                    ),
                    "outlier",
                ),
                json.dumps(
                    {
                        "column_edges": s.column_edges,
                        "column_pitch": s.column_pitch,
                        "pitch_confidence": s.pitch_confidence,
                        "text_left": s.text_left,
                        "text_right": s.text_right,
                        "band_repair": s.band_repair,
                    }
                ),
                s.pitch_confidence,
                int(len(s.bands) != template.band_count),
                run_id,
            ),
        )
        layout_id = int(cur.lastrowid)
        conn.execute("DELETE FROM bands WHERE page_layout_id = ?", (layout_id,))
        for b in s.bands:
            conn.execute(
                "INSERT INTO bands (page_layout_id, band_index, label, bbox_json) "
                "VALUES (?,?,?,?)",
                (layout_id, b.index, None, json.dumps([0, b.top, float(w), b.bottom])),
            )
    conn.commit()
    _finish_run(conn, run_id)

    out_dir = project.analysis_dir(document_id, "layout")
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "structures.json").write_text(
        json.dumps([s.to_dict() for s in structures], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    template_path = project.configs / f"template_{document_id}.yaml"
    template_path.parent.mkdir(parents=True, exist_ok=True)
    # Page overrides are verified document structure, not measurements the
    # corpus-wide template learner can reproduce.  A layout refresh must not
    # erase the ruled title-page split a reviewer has established.
    saved_overrides = None
    saved_phase_fraction = None
    saved_phase_anchor = None
    if template_path.exists():
        previous = yaml.safe_load(template_path.read_text(encoding="utf-8")) or {}
        saved_overrides = previous.get("page_overrides")
        saved_phase_fraction = previous.get("entry_phase_fraction")
        saved_phase_anchor = previous.get("entry_phase_anchor_x")
    template_data = template.to_dict()
    if saved_overrides:
        template_data["page_overrides"] = saved_overrides
    if saved_phase_fraction is not None:
        template_data["entry_phase_fraction"] = saved_phase_fraction
        # Once the book's correct sub-column orientation has been verified,
        # learn its canonical right-edge landmark from every regular scan.  The
        # median is deliberately corpus-wide: torn pages can flip their local
        # gutter vote, but a few of them cannot flip the volume.
        from foresight_ocr.segmentation.entries import infer_entry_phase_anchor

        overridden = {int(p) for p in (saved_overrides or {})}
        phase_anchor = infer_entry_phase_anchor(
            [
                {
                    "column_edges": s.column_edges,
                    "column_pitch": s.column_pitch,
                    "pitch_confidence": s.pitch_confidence,
                    "text_left": s.text_left,
                    "text_right": s.text_right,
                }
                for s in structures
                if s.page_index not in overridden
            ],
            template.column_pitch,
            template.text_left,
            template.text_right,
            float(saved_phase_fraction),
        )
        if phase_anchor is not None:
            template_data["entry_phase_anchor_x"] = phase_anchor
    elif saved_phase_anchor is not None:
        template_data["entry_phase_anchor_x"] = saved_phase_anchor
    template_path.write_text(
        yaml.safe_dump(template_data, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )

    step = max(1, len(structures) // overlays)
    rendered = []
    for s in structures[::step][:overlays]:
        gray = _imread(norm_dir / f"p{s.page_index:04d}.png")
        edges = [b.top for b in s.bands] + ([s.bands[-1].bottom] if s.bands else [])
        rendered.append(
            draw_grid_overlay(
                gray,
                edges,
                s.column_edges,
                out_dir / f"p{s.page_index:04d}.png",
                caption=(
                    f"p{s.page_index} bands={len(s.bands)} "
                    f"cols={len(s.column_edges) - 1} "
                    f"pitch={s.column_pitch:.0f}px "
                    f"conf={s.pitch_confidence:.2f}"
                ),
            )
        )
    if rendered:
        contact_sheet(rendered, out_dir / "contact_sheet.png")
    console.print(f"[green]template[/green] {template_path}")
    console.print(f"[green]overlays[/green] {out_dir}")


@app.command()
@_records_run_failure
def segment(
    document_id: str = typer.Argument(...),
    pages: Optional[str] = typer.Option(None, help="e.g. 2,3,10-20"),
    sample: int = typer.Option(
        20, help="Pages to crop when --pages is not given; 0 means every page"
    ),
    contexts: str = typer.Option("tight,medium,full", help="Crop widths to write"),
    variants: str = typer.Option(
        "maxrgb", help="Image variants to cut crops from, e.g. original,maxrgb"
    ),
) -> None:
    """Cut entry crops from normalized pages and record their provenance.

    Re-running is safe on a page someone has reviewed: the proposals are matched
    against the regions already there, so an edited box is kept and the
    detector's disagreement is recorded rather than applied.
    """
    import yaml

    from foresight_ocr.imaging.variants import VARIANTS, build_variant
    from foresight_ocr.regions import store
    from foresight_ocr.regions.crops import ensure_crop
    from foresight_ocr.regions.reconcile import Proposal, reconcile_page
    from foresight_ocr.segmentation.entries import (
        CONTEXTS,
        resolve_comb,
        segment_page,
        segment_structured_page,
        to_original_quad,
    )

    project = Project.discover()
    conn = _open_db(project)
    profile = _activate(project, document_id)
    band_of = profile.band_map()
    doc = conn.execute(
        "SELECT * FROM documents WHERE id = ?", (document_id,)
    ).fetchone()
    if doc is None:
        raise typer.BadParameter(f"unknown document {document_id!r}")

    wanted = [c.strip() for c in contexts.split(",") if c.strip()]
    unknown = set(wanted) - set(CONTEXTS)
    if unknown:
        raise typer.BadParameter(f"unknown context(s): {sorted(unknown)}")
    ctx = {k: CONTEXTS[k] for k in wanted}

    # "original" means the normalized page as-is; anything else is one of the
    # restoration variants, built from the whole page before cutting so that
    # windowed operations (background flattening, CLAHE) see page context rather
    # than a single column.
    wanted_variants = [v.strip() for v in variants.split(",") if v.strip()]
    unknown_v = set(wanted_variants) - set(VARIANTS) - {"original"}
    if unknown_v:
        raise typer.BadParameter(f"unknown variant(s): {sorted(unknown_v)}")

    rows = conn.execute(
        """SELECT pl.page_index, pl.layout_family, pl.frame_json, pl.id AS layout_id
           FROM page_layouts pl
           WHERE pl.document_id = ?
             AND pl.run_id = (SELECT MAX(id) FROM processing_runs
                              WHERE document_id = ? AND stage = 'layout'
                                AND status = 'completed')
           ORDER BY pl.page_index""",
        (document_id, document_id),
    ).fetchall()
    if not rows:
        raise typer.BadParameter("no layout results; run layout first")

    selected = _parse_pages(pages)
    # Outlier pages are segmented too: an unusual layout is a reason to look
    # harder, not a reason to drop the people printed on it. The family is
    # recorded so the reviewer knows which pages to distrust.
    usable = list(rows)
    if selected:
        targets = [r for r in rows if r["page_index"] in selected]
    elif sample:
        step = max(1, len(usable) // sample)
        targets = usable[::step][:sample]
    else:
        targets = usable

    run = ProcessingRun(
        stage="segment",
        params={
            "contexts": wanted,
            "variants": wanted_variants,
            "pages": pages or f"sample={sample}",
        },
        input_checksum=doc["checksum"],
    )
    run_id = _start_run(conn, document_id, run)

    template_path = project.configs / f"template_{document_id}.yaml"
    if not template_path.exists():
        raise typer.BadParameter(f"missing {template_path}; run layout first")
    template = yaml.safe_load(template_path.read_text(encoding="utf-8"))
    corpus_pitch = float(template["column_pitch"])
    corpus_text_left = float(template["text_left"])
    corpus_text_right = float(template["text_right"])
    phase_fraction = float(template.get("entry_phase_fraction", 0.0))
    phase_anchor_x = (
        float(template["entry_phase_anchor_x"])
        if template.get("entry_phase_anchor_x") is not None
        else None
    )
    page_overrides = {
        int(page): value
        for page, value in (template.get("page_overrides") or {}).items()
    }

    norm_dir = project.pages_dir(document_id, "normalized")
    crops_dir = project.crops_dir(document_id)
    crops_dir.mkdir(parents=True, exist_ok=True)
    written = 0
    entries_total = 0
    pitch_overrides: list[int] = []

    # Drop this document's own prior crop rows for these pages and cut them
    # again. These describe where the machine put a crop, so they are the
    # machine's to replace; the regions those crops belong to are reconciled
    # below rather than deleted, and the recognizer answers now hang off the
    # regions, so nothing said about the page is lost by re-cutting it.
    page_list = [row["page_index"] for row in targets]
    if page_list:
        marks = ",".join("?" * len(page_list))
        # ``marks`` is generated solely from the length of ``page_list``.
        conn.execute(
            f"""DELETE FROM physical_entries WHERE id IN (
                    SELECT entry_id FROM source_regions
                    WHERE document_id = ? AND page_index IN ({marks})
                )""",  # nosec B608
            (document_id, *page_list),
        )
        # ``marks`` contains question-mark placeholders, never page values.
        conn.execute(
            f"DELETE FROM source_regions WHERE document_id = ? "
            f"AND page_index IN ({marks})",  # nosec B608
            (document_id, *page_list),
        )
        conn.commit()

    reports: list[Any] = []

    with console.status(f"segmenting {len(targets)} pages…") as status:
        for n, row in enumerate(targets, 1):
            page_index = row["page_index"]
            geom = json.loads(row["frame_json"])
            page_path = norm_dir / f"p{page_index:04d}.png"
            if not page_path.exists():
                continue
            page = _imread(page_path)
            ph, pw = page.shape[:2]
            variant_pages = {
                v: build_variant(page, v) for v in wanted_variants if v != "original"
            }

            bands = conn.execute(
                "SELECT id, band_index, bbox_json FROM bands "
                "WHERE page_layout_id = ? ORDER BY band_index",
                (row["layout_id"],),
            ).fetchall()
            band_geom = []
            band_ids = {}
            for b in bands:
                x0, y0, x1, y1 = json.loads(b["bbox_json"])
                band_geom.append((b["band_index"], float(y0), float(y1)))
                band_ids[b["band_index"]] = b["id"]

            # Shared with the editor's re-cut, so a page re-cut at phase zero
            # reproduces the boxes this pass produced rather than something
            # subtly different that would read as a change.
            page_pitch, text_left, text_right, used_corpus = resolve_comb(
                geom, corpus_pitch, corpus_text_left, corpus_text_right
            )
            if used_corpus:
                pitch_overrides.append(page_index)

            override = page_overrides.get(page_index)
            if override:
                regions = segment_structured_page(
                    page_index=page_index,
                    bands=band_geom,
                    override=override,
                    page_width=pw,
                    contexts=ctx,
                )
            else:
                regions = segment_page(
                    page_index=page_index,
                    bands=band_geom,
                    column_edges=geom["column_edges"],
                    pitch=page_pitch,
                    text_left=text_left,
                    text_right=text_right,
                    page_width=pw,
                    contexts=ctx,
                    phase_offset=page_pitch * phase_fraction,
                    phase_anchor_x=phase_anchor_x,
                )
            entries_total += len(
                {(r.band_index, r.entry_index) for r in regions if r.role == "entry"}
            )

            tf = conn.execute(
                "SELECT id, inverse_json FROM transforms "
                "WHERE document_id = ? AND page_index = ? ORDER BY rowid DESC LIMIT 1",
                (document_id, page_index),
            ).fetchone()
            inverse = json.loads(tf["inverse_json"]) if tf else None

            # The tight width is the column's own extent; the others pad it by a
            # fraction of the page pitch, so they describe the neighbours as
            # much as the entry. Everything that identifies a column — the
            # physical entry, and the region it belongs to — uses the tight one.
            tightest_by_key: dict[tuple[int, int], Any] = {}
            for r in regions:
                key = (r.band_index, r.entry_index)
                best = tightest_by_key.get(key)
                if best is None or (r.x1 - r.x0) < (best.x1 - best.x0):
                    tightest_by_key[key] = r

            report = reconcile_page(
                conn,
                document_id,
                page_index,
                [
                    Proposal(
                        band_ordinal=band,
                        band_label=band_of.get(band),
                        entry_index=entry,
                        bbox=tight.bbox,
                        role=tight.role,
                        orig_quad=(
                            to_original_quad(tight.bbox, inverse) if inverse else None
                        ),
                        transform_id=tf["id"] if tf else None,
                    )
                    for (band, entry), tight in sorted(tightest_by_key.items())
                ],
                run_id=run_id,
            )
            reports.append(report)
            if report.touched:
                # Findings address the old reading order.  After a structural
                # split they would point at a different person with a confident
                # explanation, so they must wait for the next validate run.
                conn.execute(
                    "DELETE FROM validation_findings "
                    "WHERE document_id = ? AND page_index = ?",
                    (document_id, page_index),
                )

            # One physical entry per column; its several crop widths are all
            # source regions pointing back at that same entry.
            entry_rows: dict[tuple[int, int], int] = {}
            for r in regions:
                key = (r.band_index, r.entry_index)
                if key not in entry_rows:
                    tightest = tightest_by_key[key]
                    cur = conn.execute(
                        "INSERT INTO physical_entries "
                        "(band_id, entry_index, bbox_json) VALUES (?,?,?)",
                        (
                            band_ids.get(r.band_index),
                            r.entry_index,
                            json.dumps(tightest.bbox),
                        ),
                    )
                    entry_rows[key] = int(cur.lastrowid)

                crop_id = (
                    f"{document_id}_p{page_index:04d}"
                    f"_b{r.band_index}_e{r.entry_index:02d}_{r.context}"
                )
                y0, y1, x0, x1 = int(r.y0), int(r.y1), int(r.x0), int(r.x1)
                if y1 <= y0 or x1 <= x0:
                    continue

                first_path = None
                for vname in wanted_variants:
                    src = page if vname == "original" else variant_pages[vname]
                    crop = src[y0:y1, x0:x1]
                    if crop.size == 0:
                        continue
                    vdir = crops_dir / vname
                    vdir.mkdir(parents=True, exist_ok=True)
                    out = vdir / f"{crop_id}.png"
                    write_image(out, crop)
                    written += 1
                    if first_path is None:
                        first_path = out
                if first_path is None:
                    continue
                out = first_path

                conn.execute(
                    """INSERT INTO source_regions
                       (entry_id, document_id, page_index, role, context,
                        bbox_json, normalized_bbox_json, transform_id,
                        crop_id, crop_path, region_id)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        entry_rows[key],
                        document_id,
                        page_index,
                        r.role,
                        r.context,
                        json.dumps(
                            to_original_quad(r.bbox, inverse) if inverse else None
                        ),
                        json.dumps(r.bbox),
                        tf["id"] if tf else None,
                        crop_id,
                        str(out),
                        reports[-1].links.get(key),
                    ),
                )

                # The legacy positional crop above keeps benchmark exports
                # compatible.  The editor and on-demand OCR use the stable,
                # content-addressed crop attached to the region itself.
                if r.context == "tight":
                    region_id = report.links.get(key)
                    if region_id is not None:
                        uid = conn.execute(
                            "SELECT region_uid FROM regions WHERE id = ?",
                            (region_id,),
                        ).fetchone()
                        region = store.get(conn, uid["region_uid"]) if uid else None
                        if region is not None:
                            for vname in wanted_variants:
                                ensure_crop(
                                    conn,
                                    project,
                                    region,
                                    variant=vname,
                                    context="tight",
                                )
            status.update(f"segmenting {n}/{len(targets)}")

    relinked = _repoint_breadcrumbs(conn, document_id, page_list)
    conn.commit()
    _finish_run(conn, run_id)
    console.print(
        f"[green]{written}[/green] crops from {entries_total} entries "
        f"across {len(targets)} pages "
        f"({len(wanted)} width(s) × {len(wanted_variants)} variant(s)) "
        f"→ {crops_dir}"
    )
    if len(targets) < len(rows):
        # --sample defaults to 20, which is right for iterating on geometry and
        # wrong for building a book. Saying only "20 pages" reads as success;
        # saying which 20 of how many is what makes the shortfall obvious.
        console.print(
            f"[yellow]partial[/yellow] {len(targets)} of {len(rows)} pages have "
            f"crops; pass --sample 0 to segment the whole document"
        )
    if pitch_overrides:
        console.print(
            f"[cyan]{len(pitch_overrides)}[/cyan] page(s) used the corpus pitch "
            f"({corpus_pitch:.0f} px) instead of their own estimate: "
            f"{pitch_overrides[:12]}"
        )
    _report_reconcile(reports)
    if relinked:
        console.print(
            f"[dim]{relinked} existing transcription(s) re-aimed at the new crop "
            f"rows; none was discarded[/dim]"
        )


def _repoint_breadcrumbs(conn, document_id: str, pages: list[int]) -> int:
    """Re-aim `ocr_candidates.source_region_id` at the crop rows just written.

    The authoritative link is `region_id`, and it is untouched by a rerun. But
    the benchmark, the review app and `verify-layout` still read an answer
    through the crop row it was cut from, and those rows are replaced whenever a
    page is segmented again. Left stale, the answers stay in the database and
    disappear from every report — which is worse than losing them, because
    nothing says so.

    Matching is on (region, crop width): the width is what distinguishes the
    several crop rows of one column, and it is recorded against the answer by
    the crop it read.
    """
    if not pages:
        return 0
    marks = ",".join("?" * len(pages))
    # ``marks`` contains question-mark placeholders, never page values.
    rows = conn.execute(
        f"SELECT id, region_id, context FROM source_regions "
        f"WHERE document_id = ? AND page_index IN ({marks}) AND region_id IS NOT NULL",  # nosec B608
        (document_id, *pages),
    ).fetchall()
    if not rows:
        return 0

    by_region_width = {(r["region_id"], r["context"]): r["id"] for r in rows}
    by_region: dict[int, int] = {}
    for r in rows:
        by_region.setdefault(r["region_id"], r["id"])

    updates = [
        (target, c["id"])
        # ``marks`` contains question-mark placeholders, never page values.
        for c in conn.execute(
            f"SELECT c.id, c.region_id, rc.context AS context FROM ocr_candidates c "
            f"LEFT JOIN region_crops rc ON rc.crop_key = c.crop_key "
            f"WHERE c.region_id IN (SELECT region_id FROM source_regions "
            f"  WHERE document_id = ? AND page_index IN ({marks}) "
            f"    AND region_id IS NOT NULL)",  # nosec B608
            (document_id, *pages),
        )
        if (
            target := by_region_width.get((c["region_id"], c["context"]))
            or by_region.get(c["region_id"])
        )
    ]
    conn.executemany(
        "UPDATE ocr_candidates SET source_region_id = ? WHERE id = ?", updates
    )
    return len(updates)


def _report_reconcile(reports: list[Any]) -> None:
    """Say what the rerun did to the regions, not just how many crops it wrote.

    A segmentation that changed nothing and one that moved every column produce
    the same crop count, and the difference is the whole question when a page
    has already been reviewed.
    """
    if not reports:
        return
    totals = {
        k: sum(getattr(r, k) for r in reports)
        for k in ("unchanged", "moved", "created", "revived", "retired", "refused")
    }
    divergent = [(r.page_index, len(r.divergent)) for r in reports if r.divergent]
    orphaned = [(r.page_index, len(r.orphaned)) for r in reports if r.orphaned]
    locked = sorted({label for r in reports for label in r.order_locked})

    if not any(v for k, v in totals.items() if k != "unchanged"):
        console.print(
            f"[green]regions unchanged[/green]: all {totals['unchanged']} matched "
            f"what was already there"
        )
    else:
        console.print(
            "regions: "
            f"[green]{totals['unchanged']}[/green] unchanged, "
            f"[cyan]{totals['moved']}[/cyan] moved, "
            f"[cyan]{totals['created']}[/cyan] new, "
            f"[cyan]{totals['revived']}[/cyan] restored, "
            f"[yellow]{totals['retired']}[/yellow] withdrawn"
        )
    if totals["refused"]:
        console.print(
            f"[dim]{totals['refused']} proposal(s) name a column a reviewer said "
            f"is not there; not re-inserted[/dim]"
        )
    if divergent:
        n = sum(c for _, c in divergent)
        console.print(
            f"[yellow]{n}[/yellow] edited region(s) the detector now disagrees with, "
            f"kept as they are on page(s) {[p for p, _ in divergent][:12]} "
            f"— see region_proposals"
        )
    if orphaned:
        n = sum(c for _, c in orphaned)
        console.print(
            f"[yellow]{n}[/yellow] edited region(s) nothing proposed, kept on "
            f"page(s) {[p for p, _ in orphaned][:12]}"
        )
    if locked:
        console.print(
            f"[cyan]reading order left alone[/cyan] in band(s) {locked} — "
            f"a reviewer has set it"
        )


@app.command()
@_records_run_failure
def validate(
    document_id: str = typer.Argument(...),
    tag: Optional[str] = typer.Option(None, help="OCR configuration to check"),
    from_tsv: Optional[Path] = typer.Option(
        None,
        "--from-tsv",
        exists=True,
        dir_okay=False,
        help="page<TAB>band<TAB>entry<TAB>text; checks a hand-made file instead",
    ),
) -> None:
    """Check each band's ID run for gaps, reversals and duplicates.

    Findings are recorded, never repaired: a number rewritten to satisfy the
    sequence would fake the agreement this check exists to measure.

    The scoring itself is `harness.sequence_score`, the same function the
    benchmark uses. This command used to carry its own copy, which quietly
    drifted — it never learned band labels, the geometry fallback or the numeral
    repairs, and so reported a 0.9% parse rate on output the benchmark scored at
    97%. One implementation, one answer.
    """
    from foresight_ocr.ocr.harness import load_outcomes, sequence_score
    from foresight_ocr.validation.sequence import Observation, check_sequence

    project = Project.discover()
    conn = _open_db(project)
    _activate(project, document_id)
    doc = conn.execute(
        "SELECT * FROM documents WHERE id = ?", (document_id,)
    ).fetchone()
    if doc is None:
        raise typer.BadParameter(f"unknown document {document_id!r}")

    if from_tsv:
        observations = []
        for line in from_tsv.read_text(encoding="utf-8").splitlines():
            if not line.strip() or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) >= 4:
                observations.append(
                    Observation(int(parts[0]), parts[1], int(parts[2]), parts[3])
                )
        if not observations:
            raise typer.BadParameter(f"no usable rows in {from_tsv}")
        reports = check_sequence(observations)
        seq = {
            band: {
                "observed": r.observed,
                "parsed": r.parsed,
                "parse_rate": r.parse_rate,
                "clean_run_rate": r.clean_run_rate,
                "first_value": r.first_value,
                "last_value": r.last_value,
                "findings": [f.to_dict() for f in r.findings],
            }
            for band, r in sorted(reports.items())
        }
        source = str(from_tsv)
    else:
        outcomes = load_outcomes(conn, document_id)
        if tag is not None:
            outcomes = [o for o in outcomes if o.tag == tag]
        if not outcomes:
            console.print(
                "[yellow]no OCR results to validate[/yellow] — run benchmark, "
                "or pass --from-tsv for a hand-made file."
            )
            raise typer.Exit(code=0)
        # Most entries scored wins when several configurations are stored.
        outcome = max(outcomes, key=lambda o: len(o.refs))
        seq = sequence_score(outcome)
        source = f"{outcome.backend}/{outcome.variant}/{outcome.tag or 'default'}"

    run = ProcessingRun(
        stage="validate",
        params={"source": source, "tag": tag},
        input_checksum=doc["checksum"],
    )
    run_id = _start_run(conn, document_id, run)

    _clear_findings(conn, document_id, SEQUENCE_FINDING_KINDS)

    table = Table(title=f"sequential-ID validation ({source})")
    for col in ("band", "entries", "parsed", "range", "clean transitions", "findings"):
        table.add_column(col, justify="right" if col != "band" else "left")

    total = 0
    for band, raw_band in sorted(seq.items()):
        if band.startswith("_"):
            continue
        b = cast(dict[str, Any], raw_band)
        total += len(b["findings"])
        table.add_row(
            band,
            str(b["observed"]),
            f"{b['parsed']} ({b['parse_rate']:.1%})",
            f"{b['first_value']}–{b['last_value']}",
            f"{b['clean_run_rate']:.2%}",
            str(len(b["findings"])),
        )
        for f in b["findings"]:
            conn.execute(
                """INSERT INTO validation_findings
                   (run_id, document_id, band_label, kind, page_index,
                    entry_index, expected, observed, status)
                   VALUES (?,?,?,?,?,?,?,?, 'needs_review')""",
                (
                    run_id,
                    document_id,
                    band,
                    f["kind"],
                    f["page_index"],
                    f["entry_index"],
                    f["expected"],
                    f["observed"],
                ),
            )
    conn.commit()
    _finish_run(conn, run_id)
    console.print(table)

    recovered = cast(list[dict[str, Any]], seq.get("_label_recovered", []))
    if recovered:
        console.print(
            f"[cyan]{len(recovered)}[/cyan] entries took the band label from "
            "page geometry (recorded, transcription untouched)"
        )

    out = project.analysis_dir(document_id, "validation")
    out.mkdir(parents=True, exist_ok=True)
    (out / "findings.json").write_text(
        json.dumps(seq, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    console.print(
        f"{total} finding(s) → {out / 'findings.json'} "
        "(all marked needs_review; nothing was auto-corrected)"
    )


@app.command()
@_records_run_failure
def benchmark(
    document_id: str = typer.Argument(...),
    backends: str = typer.Option("ppocr_v5,paddleocr_vl", help="Comma-separated"),
    variants: str = typer.Option("maxrgb", help="Crop variants to score"),
    contexts: str = typer.Option("tight", help="Crop widths to score"),
    limit: int = typer.Option(0, help="Cap crops per combination; 0 = all"),
    backend_options: Optional[str] = typer.Option(
        None,
        "--backend-options",
        help='JSON per backend, e.g. \'{"paddleocr_vl":{"batched":true,'
        '"image_scale":0.5}}\'',
    ),
    tag: Optional[str] = typer.Option(
        None, help="Label recorded with this run, e.g. batched-0.5x"
    ),
) -> None:
    """Run every backend over identical crops and score them.

    Scoring is two independent paths: the sequence checksum (no ground truth,
    covers everything) and the hand-verified gold set (small, but the only way
    to see character-level error). Neither is allowed to correct the other.
    """
    from foresight_ocr.ocr.harness import (
        discover_crops,
        load_gold,
        load_outcomes,
        run_backend,
        store_results,
        summarize,
    )

    project = Project.discover()
    conn = _open_db(project)
    _activate(project, document_id)
    doc = conn.execute(
        "SELECT * FROM documents WHERE id = ?", (document_id,)
    ).fetchone()
    if doc is None:
        raise typer.BadParameter(f"unknown document {document_id!r}")

    backend_names = [b.strip() for b in backends.split(",") if b.strip()]
    variant_names = [v.strip() for v in variants.split(",") if v.strip()]
    context_names = [c.strip() for c in contexts.split(",") if c.strip()]
    crops_root = project.crops_dir(document_id)

    gold = load_gold(sorted((project.root / "benchmarks" / "gold").glob("*.tsv")))
    console.print(f"gold entries available: {len(gold)}")

    # Per-backend knobs (batching, image scale, …). Recorded in the run params so
    # a result can always be traced to the settings that produced it.
    per_backend: dict[str, dict] = json.loads(backend_options or "{}")
    unknown_opts = set(per_backend) - set(backend_names)
    if unknown_opts:
        raise typer.BadParameter(
            f"--backend-options names backends not being run: {sorted(unknown_opts)}"
        )

    run = ProcessingRun(
        stage="benchmark",
        params={
            "backends": backend_names,
            "variants": variant_names,
            "contexts": context_names,
            "limit": limit,
            "backend_options": per_backend,
            "tag": tag,
        },
        input_checksum=doc["checksum"],
        compute_backend="local",
    )
    run_id = _start_run(conn, document_id, run)

    collected: list[Any] = []

    for variant in variant_names:
        for context in context_names:
            refs = discover_crops(crops_root, variant, context, document_id)
            if limit:
                refs = refs[:limit]
            if not refs:
                console.print(
                    f"[yellow]no crops[/yellow] for {variant}/{context}; "
                    f"on disk: {', '.join(_crop_variants(crops_root)) or 'none'}"
                )
                continue
            for backend_name in backend_names:
                console.print(
                    f"running [bold]{backend_name}[/bold] on {len(refs)} crops "
                    f"({variant}/{context})…"
                )
                outcome = run_backend(
                    backend_name,
                    refs,
                    variant,
                    context,
                    options=per_backend.get(backend_name),
                    tag=tag or "",
                )
                collected.append(outcome)
                store_results(conn, document_id, run_id, outcome)
                conn.commit()

    _finish_run(conn, run_id)
    if not collected:
        # Every requested combination was empty. Rendering the table here would
        # print an OCR benchmark with no rows and exit successfully, which reads
        # as "the run found nothing to say" rather than "the run never ran" —
        # and it overwrites results.json on the way out.
        console.print(
            f"[red]no crops matched[/red] {variant_names} x {context_names}; "
            f"variants on disk: {', '.join(_crop_variants(crops_root)) or 'none'}. "
            "Re-run `segment` with the variant you want, or pass --variants."
        )
        raise typer.Exit(1)
    # Summarize everything stored, not just what this invocation ran. A partial
    # run — one backend, one variant — would otherwise overwrite results.json
    # with a single row and lose the rest of the grid.
    rows, agreements = summarize(load_outcomes(conn, document_id), gold)
    _render_benchmark(project, document_id, rows, agreements)


# Two stages write findings for the same document and the review app shows them
# together. Each clears only what it produced, so re-running one does not erase
# the other's — `validate` used to delete every finding for the document.
SEQUENCE_FINDING_KINDS = ("gap", "non_monotonic", "duplicate", "unparsed")
GRAPH_FINDING_KINDS = (
    "duplicate_id",
    "unresolved_father",
    "no_father_field",
    "order_conflict",
    "order_reversed",
)


def _clear_findings(conn, document_id: str, kinds: tuple[str, ...]) -> None:
    marks = ",".join("?" * len(kinds))
    # ``marks`` contains question-mark placeholders, never finding-kind values.
    conn.execute(
        f"DELETE FROM validation_findings WHERE document_id = ? AND kind IN ({marks})",  # nosec B608
        (document_id, *kinds),
    )


def _core_run(values: set[int]) -> tuple[set[int], set[int]]:
    """Split a band's ids into its contiguous run and the strays outside it.

    Ids in a band run consecutively, so the real population sits in one dense
    stretch. A recognizer error can land a value far outside it; keeping such a
    value in the range turns every integer between into a "missing" person.

    The run is bounded by the 1st and 99th percentile widened by half its own
    span — generous enough that a genuinely sparse band keeps all its members,
    tight enough that an order-of-magnitude stray is excluded.
    """
    if len(values) < 8:
        return set(values), set()
    ordered = sorted(values)
    lo = ordered[max(0, int(len(ordered) * 0.01) - 1)]
    hi = ordered[min(len(ordered) - 1, int(len(ordered) * 0.99))]
    pad = max(1, (hi - lo) // 2)
    low, high = lo - pad, hi + pad
    core = {v for v in values if low <= v <= high}
    return core, values - core


def _imread(path: Path, flags: int = 1):
    """Read an image, or say which file is unreadable and why it matters.

    OpenCV reports failure by returning None, so a truncated page — the
    kind a killed run leaves behind — surfaces hundreds of lines later as
    `'NoneType' object has no attribute 'shape'`, naming neither the file nor
    the stage. Re-running the producing stage is the fix, so the message says
    so.
    """
    img = read_image(path, flags)
    if img is None:
        raise typer.BadParameter(
            f"cannot read {path} — it is missing or truncated. "
            f"Re-run the stage that wrote it."
        )
    return img


def _crop_variants(crops_root: Path) -> list[str]:
    """Which crop variants `segment` actually wrote, for error messages."""
    if not crops_root.is_dir():
        return []
    return sorted(d.name for d in crops_root.iterdir() if d.is_dir())


def _render_benchmark(project, document_id: str, rows, agreements) -> None:
    from foresight_ocr.ocr.harness import sequence_totals

    table = Table(title="OCR benchmark")
    table.add_column("backend")
    table.add_column("tag")
    table.add_column("variant")
    table.add_column("crop")
    table.add_column("read", justify="right")
    table.add_column("id parsed", justify="right")
    table.add_column("clean run", justify="right")
    table.add_column("gold exact-id", justify="right")
    table.add_column("CER", justify="right")
    table.add_column("ms/crop", justify="right")

    for r in rows:
        parse_rate, clean = sequence_totals(r["sequence"])
        g = r["gold"]
        table.add_row(
            r["backend"],
            r.get("tag") or "—",
            r["variant"],
            r["context"],
            f"{r['read']}/{r['crops']}",
            f"{parse_rate:.1%}",
            f"{clean:.2%}",
            f"{g['field_exact']['own_id']:.1%}" if g else "—",
            f"{g['cer']:.3f}" if g else "—",
            f"{g['latency_ms_median']:.0f}" if g and g["latency_ms_median"] else "—",
        )
    console.print(table)

    for key, agr in sorted(agreements.items()):
        b1, b2, v, c = key.split("|")
        console.print(
            f"agreement {b1} vs {b2} ({v}/{c}): "
            f"[bold]{agr['own_id_agreement_rate']:.2%}[/bold] on own_id "
            f"({agr['identical_own_id']}/{agr['compared']}), "
            f"{agr['identical_text']} identical full transcriptions"
        )

    out = project.analysis_dir(document_id, "benchmark")
    out.mkdir(parents=True, exist_ok=True)
    (out / "results.json").write_text(
        json.dumps(
            {"rows": rows, "agreement": agreements}, ensure_ascii=False, indent=2
        ),
        encoding="utf-8",
    )
    console.print(f"[green]results[/green] {out / 'results.json'}")


@app.command()
def review(
    document_id: str = typer.Argument(...),
    port: int = typer.Option(8765),
    tag: Optional[str] = typer.Option(
        None, help="OCR configuration to pre-fill from; default is the latest"
    ),
    reviewer: str = typer.Option("local", help="Recorded with each correction"),
    open_browser: bool = typer.Option(True, "--open/--no-open"),
) -> None:
    """Page-at-a-time review app for verifying transcriptions."""
    from foresight_ocr.review import serve

    project = Project.discover()
    serve(
        project,
        document_id,
        port=port,
        tag=tag,
        reviewer=reviewer,
        open_browser=open_browser,
    )


@app.command("verify-layout")
def verify_layout(
    document_id: str = typer.Argument(...),
    expected_pages: int = typer.Option(200, help="Chart pages in the volume"),
    tag: Optional[str] = typer.Option(None, help="OCR configuration to use"),
    mark_headers: bool = typer.Option(
        False,
        "--mark-headers",
        help="Reclassify entries whose text is a section header, so they are "
        "not reviewed or counted as people",
    ),
) -> None:
    """Check that entries were cut correctly, independently of the lattice."""
    from foresight_ocr.layout.verify import (
        HEADER_RE,
        LayoutVerification,
        check_phantoms,
        edge_bias,
    )
    from foresight_ocr.ocr.fields import parse_entry
    from foresight_ocr.ocr.harness import BAND_LABELS
    from foresight_ocr.validation.numerals import parse_numeral

    project = Project.discover()
    conn = _open_db(project)

    rows = conn.execute(
        """SELECT sr.page_index p, b.band_index bi, pe.entry_index ei,
                  sr.crop_path cp, oc.transcription t
           FROM source_regions sr
           JOIN physical_entries pe ON pe.id = sr.entry_id
           JOIN bands b ON b.id = pe.band_id
           LEFT JOIN ocr_candidates oc ON oc.source_region_id = sr.id
           WHERE sr.document_id = ? AND sr.context = 'tight'""",
        (document_id,),
    ).fetchall()
    if not rows:
        raise typer.BadParameter("nothing segmented; run segment first")

    pages = sorted({r["p"] for r in rows})
    per_page = Counter(r["p"] for r in rows)
    missing = [p for p in range(2, expected_pages + 2) if p not in set(pages)]

    unparsed: Counter[int] = Counter()
    headers = []
    ids: dict[str, set[int]] = {}
    for r in rows:
        label = BAND_LABELS.get(r["bi"], str(r["bi"]))
        parsed = parse_entry(r["t"], own_label=label, trust_band=True)
        if parsed.own_id is None:
            unparsed[r["ei"]] += 1
        else:
            value = parse_numeral(parsed.own_id[1:])
            if value:
                ids.setdefault(label, set()).add(value)
        if r["t"] and HEADER_RE.search(r["t"]):
            headers.append(
                {"page": r["p"], "band": label, "entry": r["ei"], "text": r["t"][:40]}
            )

    crops = [Path(r["cp"]) for r in rows if r["cp"]]
    with console.status(f"checking {len(crops)} crops for phantom entries…"):
        phantoms, pct = check_phantoms(crops)

    id_ranges = {}
    for label, values in sorted(ids.items()):
        # Count gaps inside the band's plausible run, not between its extremes.
        # These ids are read with the band label trusted from geometry, so a
        # mangled numeral still yields a number: one misread 富 id of 5105
        # stretched the range to 2–5105 and reported 4001 missing people, none
        # of whom exist. The core run is what the band actually covers; values
        # outside it are named as outliers instead of inflating the gap count.
        core, outliers = _core_run(values)
        lo, hi = min(core), max(core)
        gaps = [v for v in range(lo, hi + 1) if v not in core]
        id_ranges[label] = {
            "count": len(values),
            "min": lo,
            "max": hi,
            "missing": len(gaps),
            "missing_sample": gaps[:12],
            "outliers": sorted(outliers)[:12],
            "outlier_count": len(outliers),
        }

    bias = edge_bias(dict(unparsed))
    v = LayoutVerification(
        pages_expected=expected_pages,
        pages_segmented=len(pages),
        pages_missing=missing,
        entries=len(rows),
        entries_per_page=dict(per_page),
        phantom_crops=phantoms,
        ink_percentiles=pct,
        unparsed_by_position=dict(sorted(unparsed.items())),
        edge_bias=bias,
        header_entries=headers,
        id_ranges=id_ranges,
    )
    if phantoms:
        v.problems.append(f"{len(phantoms)} crops contain almost no ink")
    if bias > 1.6:
        v.problems.append(f"failures concentrate at page edges (bias {bias:.2f})")
    if headers:
        v.problems.append(
            f"{len(headers)} entries contain header text "
            f"(pages {sorted({h['page'] for h in headers})})"
        )
    counts = set(per_page.values())
    odd_pages = {
        p: n
        for p, n in per_page.items()
        if n != max(counts, key=list(per_page.values()).count)
    }

    table = Table(title=f"layout verification — {document_id}")
    table.add_column("check")
    table.add_column("result")
    table.add_row(
        "pages segmented",
        f"{len(pages)} of {expected_pages}"
        + (f"  missing {missing}" if missing else ""),
    )
    table.add_row("entries", f"{len(rows)}  ({sorted(counts)} per page)")
    table.add_row(
        "phantom crops (<2% ink)",
        f"{len(phantoms)}   ink p1={pct.get('p1', 0):.3f} "
        f"median={pct.get('median', 0):.3f}",
    )
    table.add_row("unparsed by position", str(v.unparsed_by_position))
    table.add_row("edge bias", f"{bias:.2f}  (1.0 = none, >1.6 suspicious)")
    table.add_row("header contamination", str(len(headers)))
    for label, r in id_ranges.items():
        stray = (
            f", {r['outlier_count']} outside the run "
            f"({', '.join(str(o) for o in cast(list[object], r['outliers']))})"
            if r.get("outlier_count")
            else ""
        )
        table.add_row(
            f"ids {label}",
            f"{r['count']} distinct, {r['min']}–{r['max']}, "
            f"{r['missing']} missing{stray}",
        )
    console.print(table)

    if mark_headers and headers:
        # Evidence-based, not a hardcoded page number: the entry is reclassified
        # because its own transcription is header text. The region and its OCR
        # stay in place; only the role changes, so the decision is reversible
        # and auditable.
        for h in headers:
            conn.execute(
                """UPDATE source_regions SET role = 'header'
                   WHERE document_id = ? AND page_index = ? AND crop_id IN (
                       SELECT sr2.crop_id FROM source_regions sr2
                       JOIN physical_entries pe2 ON pe2.id = sr2.entry_id
                       JOIN bands b2 ON b2.id = pe2.band_id
                       WHERE sr2.document_id = ? AND sr2.page_index = ?
                         AND b2.band_index = ? AND pe2.entry_index = ?
                   )""",
                (
                    document_id,
                    h["page"],
                    document_id,
                    h["page"],
                    {v_: k_ for k_, v_ in BAND_LABELS.items()}.get(h["band"], -1),
                    h["entry"],
                ),
            )
        conn.commit()
        console.print(
            f"[green]reclassified[/green] {len(headers)} entries as headers "
            "(regions and OCR kept; only the role changed)"
        )
        v.problems = [p for p in v.problems if "header text" not in p]

    if v.ok:
        console.print("[green]layout verified[/green] — no structural problems found")
    else:
        for p in v.problems:
            console.print(f"[yellow]problem[/yellow] {p}")
    if odd_pages:
        console.print(
            f"[cyan]note[/cyan] {len(odd_pages)} page(s) carry a different "
            f"entry count: {dict(list(odd_pages.items())[:6])} — real on this "
            "corpus, but worth a look"
        )
    if missing:
        console.print(
            f"[cyan]note[/cyan] {len(missing)} page(s) never segmented; their "
            f"{len(missing) * 6} entries per band account for the missing ids"
        )

    out = project.analysis_dir(document_id, "layout") / "verification.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(v.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    console.print(f"[green]wrote[/green] {out}")


@app.command("review-queue")
def review_queue(
    document_id: str = typer.Argument(...),
    variant: str = typer.Option("maxrgb", help="Variant to pull crops from"),
    context: str = typer.Option("tight", help="Crop width to review"),
    limit: int = typer.Option(60, help="Maximum crops to queue"),
) -> None:
    """Collect the crops the checksum and backend disagreement flag.

    Hand-verification is the scarce resource, so it should be spent where the
    errors are rather than on randomly sampled entries. Emits a contact sheet
    plus a pre-filled TSV whose transcription column is left blank — the machine
    guess is shown beside it for reference but never written into the gold file.
    """

    from foresight_ocr.imaging.overlay import contact_sheet
    from foresight_ocr.ocr.harness import BAND_LABELS

    project = Project.discover()
    results_path = project.analysis_dir(document_id, "benchmark") / "results.json"
    if not results_path.exists():
        raise typer.BadParameter("no benchmark results; run benchmark first")
    data = json.loads(results_path.read_text(encoding="utf-8"))

    # Anything the sequence check flagged, plus anything the backends read
    # differently: two independent reasons to doubt a crop.
    suspects: dict[tuple[int, str, int], str] = {}
    for row in data["rows"]:
        if row["variant"] != variant or row["context"] != context:
            continue
        for band, b in row["sequence"].items():
            if band.startswith("_"):
                continue
            for f in b["findings"]:
                key = (f["page_index"], band, f["entry_index"])
                suspects.setdefault(key, f"{f['kind']}: {f['observed'] or '∅'}")

    label_to_band = {v: k for k, v in BAND_LABELS.items()}
    for key, agr in data.get("agreement", {}).items():
        _, _, v, c = key.split("|")
        if v != variant or c != context:
            continue
        for d in agr.get("disagreements", []):
            parts = d["crop_id"].rsplit("_", 4)
            if len(parts) < 5:
                continue
            page = int(parts[1].lstrip("p"))
            band_idx = int(parts[2].lstrip("b"))
            entry = int(parts[3].lstrip("e"))
            band = BAND_LABELS.get(band_idx, str(band_idx))
            reading = " vs ".join(f"{k}={v2}" for k, v2 in d.items() if k != "crop_id")
            suspects.setdefault((page, band, entry), f"disagreement: {reading}")

    if not suspects:
        console.print("[green]nothing flagged[/green] — no crops to review")
        raise typer.Exit(code=0)

    ordered = sorted(suspects.items())[:limit]
    crops_root = project.crops_dir(document_id) / variant
    out_dir = project.analysis_dir(document_id, "review")
    out_dir.mkdir(parents=True, exist_ok=True)

    paths, lines = [], []
    lines.append(
        "# Review queue: crops flagged by the sequence check or by backend "
        "disagreement."
    )
    lines.append(
        "# Fill the 4th column by reading the crop. Leave a row out "
        "entirely if you cannot read it with confidence —"
    )
    lines.append("# a guessed gold entry is worse than a missing one.")
    lines.append("# page\tband\tentry\ttranscription\t(reason / machine guess)")
    for (page, band, entry), reason in ordered:
        selected_band_idx = label_to_band.get(band)
        if selected_band_idx is None:
            continue
        crop = crops_root / (
            f"{document_id}_p{page:04d}_b{selected_band_idx}_e{entry:02d}_{context}.png"
        )
        if crop.exists():
            paths.append(crop)
        lines.append(f"{page}\t{band}\t{entry}\t\t{reason}")

    tsv = out_dir / f"review_{variant}_{context}.tsv"
    tsv.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if paths:
        contact_sheet(paths[:40], out_dir / "review_sheet.png", cols=8, cell=(150, 420))
    console.print(
        f"[yellow]{len(ordered)}[/yellow] flagged crop(s) → {tsv}"
        + (f"\nsheet: {out_dir / 'review_sheet.png'}" if paths else "")
    )


@app.command("report-ocr")
def report_ocr(document_id: str = typer.Argument(...)) -> None:
    """Write docs/ocr-benchmark.md from the benchmark artifacts."""
    from foresight_ocr.ocr.report import write_ocr_benchmark_report

    project = Project.discover()
    path = write_ocr_benchmark_report(project, document_id)
    console.print(f"[green]wrote[/green] {path}")


@app.command("learn-ocr")
def learn_ocr(
    document_id: str = typer.Argument(...),
    tag: Optional[str] = typer.Option(None, help="OCR tag to score; newest by default"),
) -> None:
    """Turn current human corrections into gold data and an OCR scorecard."""
    from foresight_ocr.ocr.learning import write_learning_report
    from foresight_ocr.review.data import export_verified

    project = Project.discover()
    conn = _open_db(project)
    profile = _activate(project, document_id)
    if (
        conn.execute("SELECT 1 FROM documents WHERE id = ?", (document_id,)).fetchone()
        is None
    ):
        raise typer.BadParameter(f"unknown document {document_id!r}")

    gold = project.root / "benchmarks" / "gold" / f"{document_id}_verified.tsv"
    verified = export_verified(
        conn, document_id, gold, generation_labels=profile.band_labels
    )
    json_path, md_path, report = write_learning_report(project, conn, document_id, tag)
    n = report.eligible_entries
    console.print(
        f"[green]{verified}[/green] verified rows -> {gold}\n"
        f"core exact: {report.exact_core_entries}/{n} "
        f"({report.exact_core_entries / n:.1%})\n"
        f"report: {md_path}\nraw evidence: {json_path}"
        if n
        else f"[yellow]no complete corrected entries[/yellow]\nreport: {md_path}"
    )


@app.command()
def rescore(document_id: str = typer.Argument(...)) -> None:
    """Recompute benchmark metrics from stored OCR results.

    Scoring is cheap; recognition is not. Correcting the gold set, fixing a
    metric or adding a field should never cost another pass over the models.
    """
    from foresight_ocr.ocr.harness import load_gold, load_outcomes, summarize

    project = Project.discover()
    conn = _open_db(project)
    outcomes = load_outcomes(conn, document_id)
    if not outcomes:
        raise typer.BadParameter("no stored OCR results; run benchmark first")

    gold = load_gold(sorted((project.root / "benchmarks" / "gold").glob("*.tsv")))
    console.print(
        f"rescoring {len(outcomes)} stored run(s) against {len(gold)} gold entries"
    )
    rows, agreements = summarize(outcomes, gold)
    _render_benchmark(project, document_id, rows, agreements)


@app.command()
def report(document_id: str = typer.Argument(...)) -> None:
    """Write docs/layout-poc-report.md from the artifacts on disk."""
    from foresight_ocr.report import write_layout_poc_report

    project = Project.discover()
    path = write_layout_poc_report(project, document_id)
    console.print(f"[green]wrote[/green] {path}")


@app.command("db")
def db_info() -> None:
    """Show what the pipeline has produced so far."""
    project = Project.discover()
    conn = _open_db(project)
    table = Table(title=str(project.db_path))
    table.add_column("stage")
    table.add_column("status")
    table.add_column("started")
    table.add_column("params")
    for row in conn.execute(
        "SELECT stage, status, started_at, params_json FROM processing_runs "
        "ORDER BY id DESC LIMIT 20"
    ):
        params = json.loads(row["params_json"])
        table.add_row(
            row["stage"],
            row["status"],
            row["started_at"][:19],
            ", ".join(f"{k}={v}" for k, v in list(params.items())[:3]),
        )
    console.print(table)


@app.command()
@_records_run_failure
def graph(
    document_id: str = typer.Argument(...),
    tag: Optional[str] = typer.Option(None, help="OCR configuration to build from"),
    export: bool = typer.Option(True, help="Write persons.tsv and a GEDCOM"),
) -> None:
    """Reconstruct the people and the father links, and check them.

    This is where the document becomes a family record. It is also the strongest
    check available without ground truth: every father id must resolve, every id
    must belong to one person, and sons of one father must carry distinct
    ascending ranks. A recognizer error breaks one of those far more often than
    it produces a plausible wrong answer.

    Derived and rebuilt from scratch each run, preferring a human correction
    over the recognizer for any entry that has one. Corrections belong in the
    review app; nothing here edits a transcription.
    """
    from foresight_ocr.genealogy import build_entries, store_graph
    from foresight_ocr.genealogy.export import write_gedcom, write_tsv
    from foresight_ocr.ocr.watermarks import filter_watermark_text

    project = Project.discover()
    conn = _open_db(project)
    profile = _activate(project, document_id)
    if (
        conn.execute("SELECT 1 FROM documents WHERE id = ?", (document_id,)).fetchone()
        is None
    ):
        raise typer.BadParameter(f"unknown document {document_id!r}")

    # Read the entries from the regions rather than from the crop rows they were
    # cut from. A crop row is replaced whenever the page is segmented again, so
    # reading through it meant a rerun emptied the graph for those pages while
    # every transcription was still on record.
    q = """SELECT r.region_uid AS region_uid, r.page_index AS page_index,
                  r.band_ordinal AS band_index, r.reading_order AS entry_index,
                  oc.transcription AS ocr_text
           FROM regions r
           LEFT JOIN pages pg
             ON pg.document_id = r.document_id
            AND pg.page_index = r.page_index
           LEFT JOIN ocr_candidates oc ON oc.region_id = r.id
           LEFT JOIN ocr_runs orun ON orun.id = oc.ocr_run_id
           WHERE r.document_id = ? AND r.deleted_at IS NULL
             AND r.role = 'entry'
             AND COALESCE(pg.ignored, 0) = 0"""
    params: list[Any] = [document_id]
    if tag:
        q += " AND orun.tag = ?"
        params.append(tag)
    q += " ORDER BY r.page_index, r.band_ordinal, r.reading_order, oc.id"

    # Corrections are keyed by band *label*, entries by band *index*, so they
    # are matched here rather than in SQL. Joining on (page, entry) alone — as
    # this did — applied one correction to the same entry position in every
    # band, silently overwriting two other people per fix.
    band_of = profile.band_map()
    corrections: dict[tuple[int, str | None, int], str | None] = {}
    for r in conn.execute(
        "SELECT page_index, band_label, entry_index, transcription "
        "FROM human_corrections WHERE document_id = ? AND role = 'entry' "
        "AND unreadable = 0",
        (document_id,),
    ):
        corrections[(r["page_index"], r["band_label"], r["entry_index"])] = r[
            "transcription"
        ]

    # One row per entry: the last OCR answer wins, a human correction beats it.
    best: dict[tuple[int, int, int], dict[str, Any]] = {}
    for r in conn.execute(q, params):
        key = (r["page_index"], r["band_index"], r["entry_index"])
        correction_key = (
            r["page_index"],
            band_of.get(r["band_index"]),
            r["entry_index"],
        )
        text: str | None
        if correction_key in corrections:
            # Presence, not truthiness, is the decision. An intentionally blank
            # correction is verified human data and must suppress the machine
            # text just as surely as a non-empty correction does. Normalise an
            # old readable NULL row to the same explicit blank semantics.
            text = corrections[correction_key] or ""
            source = "human"
        else:
            text = filter_watermark_text(r["ocr_text"]).transcription
            source = "ocr"
        if text is None and key in best:
            continue
        best[key] = {
            "region_uid": r["region_uid"],
            "page_index": r["page_index"],
            "band_index": r["band_index"],
            "entry_index": r["entry_index"],
            "text": text,
            "source": source,
        }
    if not best:
        raise typer.BadParameter(
            "no transcriptions to build from; run segment and benchmark first"
        )
    if not any(r["text"] for r in best.values()):
        # Entries exist but none has been read yet. Building anyway produces
        # "0 people, no structural findings", which reads as a clean result for
        # a document nobody has transcribed.
        raise typer.BadParameter(
            f"{len(best)} entries are segmented but none has a transcription; "
            f"run `foresight-ocr benchmark {document_id}` first"
        )

    parent_of = {label: profile.parent_of(label) for label in profile.band_labels}
    entries = build_entries(best.values(), band_of, parent_of)

    run = ProcessingRun(
        stage="graph",
        params={"tag": tag, "labels": profile.band_labels},
        input_checksum=document_id,
        compute_backend="local",
    )
    run_id = _start_run(conn, document_id, run)
    result = store_graph(
        conn, document_id, entries, parent_of, charted=set(profile.band_labels)
    )

    # The structural checks are only useful if they reach the person doing the
    # reviewing, and the review app reads validation_findings. Without this the
    # strongest signal available — a father who does not exist, two people
    # sharing an id — stayed in a terminal table nobody has open while reviewing.
    _clear_findings(conn, document_id, GRAPH_FINDING_KINDS)
    for f in result.findings:
        conn.execute(
            """INSERT INTO validation_findings
               (run_id, document_id, band_label, kind, page_index,
                entry_index, expected, observed, status)
               VALUES (?,?,?,?,?,?,?,?, 'needs_review')""",
            (
                run_id,
                document_id,
                f.band_label,
                f.kind,
                f.page_index,
                f.entry_index,
                f.expected,
                f.observed,
            ),
        )
    conn.commit()
    _finish_run(conn, run_id)

    table = Table(title=f"reconstructed genealogy — {document_id}")
    table.add_column("generation")
    table.add_column("people", justify="right")
    table.add_column("father resolved", justify="right")
    per_gen_resolved = {
        r["generation"]: r["n"]
        for r in conn.execute(
            "SELECT generation, COUNT(*) n FROM persons "
            "WHERE document_id = ? AND link_status = 'resolved' GROUP BY generation",
            (document_id,),
        )
    }
    for label in profile.band_labels:
        total = result.by_generation.get(label, 0)
        got = per_gen_resolved.get(label, 0)
        share = f"{got}/{total}" + (f"  ({got / total:.1%})" if total else "")
        table.add_row(label, str(total), share)
    console.print(table)

    counts = result.counts()
    console.print(
        f"[green]{result.people}[/green] people from {result.entries} entries; "
        f"links: "
        + ", ".join(f"{k} {v}" for k, v in sorted(result.link_status.items()))
    )
    if counts:
        console.print(
            "findings: "
            + ", ".join(f"[yellow]{k}[/yellow] {v}" for k, v in sorted(counts.items()))
        )
        for f in result.findings[:8]:
            console.print(
                f"  {f.kind} p{f.page_index} {f.band_label}#{f.entry_index}: "
                f"expected {f.expected} — read {f.observed}"
            )
        if len(result.findings) > 8:
            console.print(f"  … {len(result.findings) - 8} more")
    else:
        console.print("[green]no structural findings[/green]")

    if export:
        out = project.analysis_dir(document_id, "genealogy")
        n = write_tsv(
            conn,
            document_id,
            out / "persons.tsv",
            generation_labels=profile.band_labels,
        )
        indi, fams = write_gedcom(
            conn,
            document_id,
            out / f"{document_id}.ged",
            generation_labels=profile.band_labels,
        )
        console.print(
            f"[green]exported[/green] {n} people → {out / 'persons.tsv'}; "
            f"{indi} individuals in {fams} families → "
            f"{out / (document_id + '.ged')}"
        )


region_app = typer.Typer(help="Inspect and edit the regions of a page.")
app.add_typer(region_app, name="region")


@region_app.command("list")
def region_list(
    document_id: str,
    page: int,
    variant: str = typer.Option("maxrgb", help="crop variant to report"),
) -> None:
    """The regions of one page, in reading order, with their current text."""
    from foresight_ocr.ocr.watermarks import filter_watermark_text
    from foresight_ocr.regions import store as region_store

    project = Project.discover()
    conn = _open_db(project)
    _activate(project, document_id)

    regions = region_store.for_page(conn, document_id, page)
    if not regions:
        raise typer.BadParameter(
            f"no regions on {document_id} page {page}; run `foresight-ocr segment` first"
        )

    table = Table(title=f"{document_id} p{page}")
    for column in ("uid", "band", "order", "state", "box", "text"):
        table.add_column(column)
    for region in regions:
        row = conn.execute(
            "SELECT transcription FROM ocr_candidates WHERE region_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (region.id,),
        ).fetchone()
        x0, y0, x1, y1 = region.bbox
        table.add_row(
            region.region_uid[:12],
            region.band_label or "—",
            str(region.reading_order),
            region.state,
            f"{x0:.0f},{y0:.0f} {x1:.0f},{y1:.0f}",
            (filter_watermark_text(row["transcription"]).transcription if row else None)
            or "—",
        )
    console.print(table)


@region_app.command("move")
def region_move(
    region_uid: str,
    x0: float,
    y0: float,
    x1: float,
    y1: float,
    reocr: bool = typer.Option(False, "--reocr", help="re-read the moved region"),
    backend: str = typer.Option("paddleocr_vl", help="recognizer to re-read with"),
    variant: str = typer.Option("watermark", help="crop variant to cut"),
    reviewer: str = typer.Option("local", help="who is making this change"),
) -> None:
    """Move or resize one region, and optionally re-read just that region.

    The command exists to make the editing model checkable without a browser:
    exactly one crop should be cut and exactly one answer stored, with every
    other region on the page — and every earlier answer about this one —
    untouched.
    """
    from foresight_ocr.ocr.ondemand import recognize_regions
    from foresight_ocr.regions import store as region_store

    project = Project.discover()
    conn = _open_db(project)

    region = region_store.get(conn, region_uid)
    if region is None:
        # Accept a prefix: the uid is opaque, and nobody should have to type 32
        # hex characters to move a box.
        matches = conn.execute(
            "SELECT region_uid FROM regions WHERE region_uid LIKE ? LIMIT 2",
            (region_uid + "%",),
        ).fetchall()
        if len(matches) != 1:
            raise typer.BadParameter(
                f"no single region matches {region_uid!r} ({len(matches)} candidates)"
            )
        region_uid = matches[0]["region_uid"]
        region = region_store.get(conn, region_uid)
        assert region is not None

    _activate(project, region.document_id)
    before = list(region.bbox)
    edit = region_store.set_geometry(conn, region_uid, [x0, y0, x1, y1], actor=reviewer)
    conn.commit()
    console.print(
        f"{region_uid[:12]}  {before} → {edit.region.bbox}  "
        f"state={edit.region.state}  undo={edit.undo['bbox']}"
    )

    if not reocr:
        console.print(
            "[dim]not re-read; pass --reocr to refresh the transcription[/dim]"
        )
        return

    answers = recognize_regions(
        conn,
        project,
        region.document_id,
        [region_uid],
        backend=backend,
        variant=variant,
    )
    conn.commit()
    for answer in answers:
        if answer.error:
            console.print(f"[red]{answer.error}[/red]")
        else:
            console.print(
                f"[green]{answer.transcription or '—'}[/green] "
                f"({'cached' if answer.reused else 'freshly read'})"
            )


page_app = typer.Typer(help="Work on the layout of a single page.")
app.add_typer(page_app, name="page")


@page_app.command("comb")
def page_comb(
    document_id: str,
    page: int,
    phase: float = typer.Option(0.0, help="shift every boundary by this many px"),
    pitch: Optional[float] = typer.Option(None, help="override the column pitch"),
    snap: bool = typer.Option(True, help="let boundaries settle onto detected gutters"),
    left: Optional[float] = typer.Option(None, help="left edge the lattice stops at"),
    right: Optional[float] = typer.Option(
        None, help="right edge the lattice starts at"
    ),
) -> None:
    """Where the column boundaries would fall, without changing anything."""
    from foresight_ocr.regions.recut import comb_inputs, plan_comb

    project = Project.discover()
    conn = _open_db(project)
    _activate(project, document_id)

    inputs = comb_inputs(conn, project, document_id, page)
    plan = plan_comb(
        inputs,
        phase_offset=phase,
        pitch=pitch,
        snap=snap,
        text_left=left,
        text_right=right,
    )
    console.print(
        f"pitch {plan.pitch:.1f} px (page measured {inputs.pitch:.1f}, corpus "
        f"{inputs.corpus_pitch:.1f}, confidence {inputs.pitch_confidence:.2f}"
        f"{', corpus used' if inputs.used_corpus_pitch else ''})"
    )
    console.print(
        f"{len(inputs.gutters)} gutters detected; "
        f"{sum(plan.snapped)} of {len(plan.boundaries)} boundaries sit on one"
    )
    console.print(
        f"[green]{plan.entries_per_band}[/green] entries per band × "
        f"{len(inputs.bands)} bands = {len(plan.proposals)} columns"
    )
    edges = sorted(plan.boundaries)
    console.print("boundaries: " + " ".join(f"{b:.0f}" for b in edges))
    if len(edges) > 1:
        gaps = [b - a for a, b in zip(edges, edges[1:], strict=False)]
        console.print("spacing:    " + " ".join(f"{g:.0f}" for g in gaps))


@page_app.command("recut")
def page_recut(
    document_id: str,
    page: int,
    phase: float = typer.Option(0.0, help="shift every boundary by this many px"),
    pitch: Optional[float] = typer.Option(None, help="override the column pitch"),
    snap: bool = typer.Option(True, help="let boundaries settle onto detected gutters"),
    left: Optional[float] = typer.Option(None, help="left edge the lattice stops at"),
    right: Optional[float] = typer.Option(
        None, help="right edge the lattice starts at"
    ),
    reocr: bool = typer.Option(True, help="re-read the columns that moved"),
    backend: str = typer.Option("paddleocr_vl", help="recognizer to re-read with"),
    variant: str = typer.Option("watermark", help="crop variant to cut"),
    reviewer: str = typer.Option("local", help="who is making this change"),
) -> None:
    """Move a page's columns onto a corrected lattice, then cut and read them.

    For the page where every entry came back wrong because the comb locked onto
    the wrong phase — one number, one pass, instead of dragging each box.
    """
    from foresight_ocr.regions.recut import apply_comb, comb_inputs, plan_comb

    project = Project.discover()
    conn = _open_db(project)
    _activate(project, document_id)

    inputs = comb_inputs(conn, project, document_id, page)
    plan = plan_comb(
        inputs,
        phase_offset=phase,
        pitch=pitch,
        snap=snap,
        text_left=left,
        text_right=right,
    )
    with console.status(f"re-cutting p{page}…"):
        report = apply_comb(
            conn,
            project,
            plan,
            inputs,
            actor=reviewer,
            reocr=reocr,
            backend=backend,
            variant=variant,
        )
    console.print(report.summary())
    if report.findings_cleared:
        console.print(
            f"[dim]{report.findings_cleared} finding(s) dropped; they described "
            f"entry positions this re-cut has changed — run `validate` again[/dim]"
        )
    if report.corrections_rekeyed:
        console.print(
            f"[dim]{report.corrections_rekeyed} correction(s) followed their "
            f"column to its new position[/dim]"
        )
    if report.corrections_stranded:
        console.print(
            f"[yellow]{report.corrections_stranded}[/yellow] correction(s) belong "
            f"to columns this re-cut withdrew; they are kept but no longer shown"
        )
    for message in report.errors[:5]:
        console.print(f"[red]{message}[/red]")


if __name__ == "__main__":
    main()

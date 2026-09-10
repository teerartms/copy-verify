#!/usr/bin/env python3
"""
verify_copy.py - Confirm a copy is byte-perfect, using hashes.

Author : Tirta Wihadi
Version: 1.0.0
License: MIT

Two modes:

  1. Manifest mode - verify a destination against a manifest produced by copyverify.py:
         python verify_copy.py --manifest <manifest.json> <dst>

  2. Pair mode - compare a source tree/file directly against a destination tree/file:
         python verify_copy.py <src> <dst> [--hash sha256]

Checks per file:
    - Destination file exists
    - Size matches
    - Hash matches (the manifest's hash, or a freshly computed source hash)

Global checks:
    - File count matches
    - Total byte count matches

Exit codes:
    0  All checks passed
    1  Argument or file error
    2  One or more checks failed
"""

__author__  = "Tirta Wihadi"
__version__ = "1.0.0"
__license__ = "MIT"

import os
import sys
import json
import time
import hashlib
import argparse
from datetime import timedelta
from pathlib import Path

from rich.console import Console
from rich.progress import (
    Progress, BarColumn, FileSizeColumn, TotalFileSizeColumn,
    TransferSpeedColumn, TextColumn, SpinnerColumn,
)
from rich.table import Table
from rich.panel import Panel
from rich import box

console = Console()

READ_CHUNK = 1024 * 1024


def format_size(num_bytes: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if num_bytes < 1024:
            return f"{num_bytes:.2f} {unit}"
        num_bytes /= 1024
    return f"{num_bytes:.2f} PB"


def hash_file(path: str, algo: str, progress=None, task=None) -> str:
    h = hashlib.new(algo)
    with open(path, "rb") as f:
        while True:
            buf = f.read(READ_CHUNK)
            if not buf:
                break
            h.update(buf)
            if progress is not None and task is not None:
                progress.advance(task, len(buf))
    return h.hexdigest()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify that a copied file / folder matches the source (byte-perfect).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("src", nargs="?", help="Source file or directory (pair mode)")
    parser.add_argument("dst", help="Destination file or directory")
    parser.add_argument("--manifest", metavar="PATH",
                        help="Manifest JSON from copyverify.py (manifest mode)")
    parser.add_argument("--hash", choices=["sha256", "md5"], default="sha256",
                        help="Hash algorithm for pair mode (default: sha256)")
    parser.add_argument("--version", action="version",
                        version=f"copy-verify {__version__}")
    return parser.parse_args()


def collect_pairs_from_src(src: Path, dst: Path) -> list:
    """Return list of (rel, src_path, dst_path, size)."""
    if src.is_dir():
        out = []
        for dirpath, dirnames, filenames in os.walk(src, followlinks=False):
            dirnames.sort()
            for name in sorted(filenames):
                abs_src = Path(dirpath) / name
                rel = abs_src.relative_to(src).as_posix()
                target = dst / rel
                out.append((rel, str(abs_src), str(target), abs_src.stat().st_size))
        return out
    target = dst / src.name if dst.is_dir() else dst
    return [(src.name, str(src), str(target), src.stat().st_size)]


def collect_from_manifest(manifest_path: Path, dst: Path) -> tuple:
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    algo = data.get("hash", "sha256")
    entries = []
    for item in data.get("files", []):
        rel = item["path"]
        target = dst / rel if dst.is_dir() else dst
        entries.append((rel, item.get(algo, ""), item["size"],
                        str(target), item.get("status", "")))
    return algo, entries, data


def main() -> None:
    args = parse_args()
    dst = Path(args.dst).resolve()

    checks = []
    failed = False

    def add_check(label, ok, detail=""):
        nonlocal failed
        checks.append((label, ok, detail))
        if not ok:
            failed = True

    start = time.monotonic()

    # ---- Build the work list --------------------------------------------------
    if args.manifest:
        manifest_path = Path(args.manifest).resolve()
        if not manifest_path.is_file():
            console.print(f"[bold red][X] Manifest not found:[/bold red] {manifest_path}")
            sys.exit(1)
        algo, man_entries, man_data = collect_from_manifest(manifest_path, dst)
        mode_label = "manifest"
        expected_files = man_data.get("totals", {}).get("files", len(man_entries))
        expected_bytes = man_data.get("totals", {}).get("bytes",
                                                        sum(e[2] for e in man_entries))
        source_label = man_data.get("source", "(from manifest)")
        # entries: (rel, expected_hash, size, dst_path, status)
        work = [(rel, None, exp_hash, size, dpath, status)
                for (rel, exp_hash, size, dpath, status) in man_entries]
    else:
        if not args.src:
            console.print("[bold red][X] Provide <src> (pair mode) or --manifest.[/bold red]")
            sys.exit(1)
        src = Path(args.src).resolve()
        if not src.exists():
            console.print(f"[bold red][X] Source not found:[/bold red] {src}")
            sys.exit(1)
        algo = args.hash
        mode_label = "pair"
        source_label = str(src)
        pairs = collect_pairs_from_src(src, dst)
        expected_files = len(pairs)
        expected_bytes = sum(p[3] for p in pairs)
        # work: (rel, src_path, expected_hash(None), size, dst_path, status)
        work = [(rel, spath, None, size, dpath, "") for (rel, spath, dpath, size) in pairs]

    if not dst.exists():
        console.print(f"[bold red][X] Destination not found:[/bold red] {dst}")
        sys.exit(1)
    if not work:
        console.print("[bold red][X] Nothing to verify.[/bold red]")
        sys.exit(1)

    info = Table(box=box.ROUNDED, show_header=False, padding=(0, 2))
    info.add_column(style="bold cyan", no_wrap=True)
    info.add_column(style="white")
    info.add_row("Mode",           mode_label)
    info.add_row("Source",         source_label)
    info.add_row("Destination",    str(dst))
    info.add_row("Files expected", f"{expected_files:,}")
    info.add_row("Bytes expected", format_size(expected_bytes))
    info.add_row("Hash",           algo.upper())
    console.print(Panel(info, title="[bold]copy-verify - verifier[/bold]", border_style="cyan"))

    # ---- Verify -------------------------------------------------------------
    total_to_read = sum(w[3] for w in work)
    matched_files = 0
    matched_bytes = 0
    row_results = []

    progress = Progress(
        SpinnerColumn(),
        TextColumn("[cyan]{task.description}"),
        BarColumn(bar_width=38),
        FileSizeColumn(), TextColumn("/"), TotalFileSizeColumn(),
        TransferSpeedColumn(),
        console=console,
        refresh_per_second=10,
    )

    with progress:
        task = progress.add_task("Verifying destination...", total=max(total_to_read, 1))

        for rel, src_path, expected_hash, size, dst_path, status in work:
            dpath = Path(dst_path)

            if status == "skipped":
                row_results.append((rel, "-", "skipped at copy time"))
                continue

            if not dpath.is_file():
                add_check(f"{rel} -> exists", False, "missing")
                row_results.append((rel, "MISSING", ""))
                continue

            actual_size = dpath.stat().st_size
            size_ok = actual_size == size
            if not size_ok:
                add_check(f"{rel} -> size", False,
                          f"expected {format_size(size)}, got {format_size(actual_size)}")

            want = expected_hash
            if want is None and src_path:
                want = hash_file(src_path, algo)  # compute source hash on the fly

            got = hash_file(dst_path, algo, progress, task)
            hash_ok = bool(want) and got == want
            add_check(
                f"{rel} -> {algo}",
                hash_ok,
                f"{got[:16]}..." if hash_ok
                else (f"want {(want or '?')[:16]}... got {got[:16]}..."),
            )
            row_results.append((rel, "OK" if (hash_ok and size_ok) else "FAIL", got[:16] + "..."))
            if hash_ok and size_ok:
                matched_files += 1
                matched_bytes += actual_size

    add_check(
        "File count (expected vs matched)",
        matched_files == expected_files,
        f"{matched_files:,} / {expected_files:,}",
    )
    add_check(
        "Byte count (expected vs matched)",
        matched_bytes == expected_bytes,
        f"{format_size(matched_bytes)} / {format_size(expected_bytes)}"
        if matched_bytes != expected_bytes
        else f"{format_size(expected_bytes)} byte-perfect",
    )

    elapsed = time.monotonic() - start

    # ---- Report ----------------------------------------------------------------
    console.print()
    files_table = Table(title="Per-file results", box=box.ROUNDED, border_style="dim")
    files_table.add_column("File", style="white")
    files_table.add_column("Result", justify="center")
    files_table.add_column(f"{algo}", style="dim")
    for rel, res, h in row_results[:400]:
        color = {"OK": "green", "FAIL": "red", "MISSING": "red"}.get(res, "yellow")
        files_table.add_row(rel, f"[{color}]{res}[/{color}]", h)
    if len(row_results) > 400:
        files_table.add_row(f"[dim]... {len(row_results) - 400:,} more[/dim]", "", "")
    console.print(files_table)

    check_table = Table(
        title="Verification Checks", box=box.ROUNDED,
        border_style="green" if not failed else "red",
    )
    check_table.add_column("Check",  style="white")
    check_table.add_column("Status", justify="center")
    check_table.add_column("Detail", style="dim")
    for label, ok, detail in checks:
        status_txt = "[bold green]PASS[/bold green]" if ok else "[bold red]FAIL[/bold red]"
        check_table.add_row(label, status_txt, detail)
    console.print(check_table)

    stats = Table(box=box.SIMPLE, show_header=False, padding=(0, 2))
    stats.add_column(style="bold dim")
    stats.add_column(style="white")
    stats.add_row("Files matched", f"{matched_files:,} / {expected_files:,}")
    stats.add_row("Bytes matched", format_size(matched_bytes))
    stats.add_row("Elapsed",       str(timedelta(seconds=int(elapsed))))
    console.print(stats)

    if not failed:
        console.print(Panel(
            "[bold green][OK] All checks passed.[/bold green]\n"
            "The destination is a byte-perfect copy of the source.",
            border_style="green",
        ))
        sys.exit(0)
    console.print(Panel(
        "[bold red][X] One or more checks FAILED.[/bold red]\n"
        "The destination does not fully match the source. Investigate before trusting it.",
        border_style="red",
    ))
    sys.exit(2)


if __name__ == "__main__":
    main()

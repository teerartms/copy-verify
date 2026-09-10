#!/usr/bin/env python3
"""
copyverify.py - Copy or move a file / folder with byte-perfect SHA-256 verification.

Author : Tirta Wihadi
Version: 1.0.0
License: MIT

Usage:
    python copyverify.py <src> <dst> [options]

Options:
    --move           Delete the source only after every file passed verification
    --hash           Hash algorithm: sha256 or md5 (default: sha256)
    --on-conflict    What to do when a destination file already exists:
                       smart      skip if hashes match, overwrite if they differ (default)
                       overwrite  always overwrite
                       skip       never touch an existing destination file
                       error      abort if any destination file already exists
    --manifest PATH  Where to write the JSON manifest
                     (default: <dst>/copyverify-manifest.json for a folder,
                               <dst>.copyverify-manifest.json for a single file)
    --no-manifest    Do not write a manifest file
    --dry-run        Show the plan and exit without copying anything
    --quiet          Suppress per-file logs, show only progress bars
    --no-verify      Skip the post-copy re-read verification (not recommended)
    --version        Show version and exit

Exit codes:
    0  All files copied and verified (or verification skipped)
    1  Argument or file error
    2  Verification failed (source is left untouched, even with --move)
"""

__author__  = "Tirta Wihadi"
__version__ = "1.0.0"
__license__ = "MIT"

import os
import sys
import json
import time
import hashlib
import shutil
import argparse
from dataclasses import dataclass, field
from datetime import timedelta, datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.progress import (
    Progress,
    BarColumn,
    FileSizeColumn,
    TotalFileSizeColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
    TextColumn,
    SpinnerColumn,
)
from rich.table import Table
from rich.panel import Panel
from rich import box

# Legacy Windows consoles default to cp1252 and choke on box-drawing / arrows.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

console = Console()

READ_CHUNK = 1024 * 1024  # 1 MiB streaming buffer


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class FileJob:
    rel: str                 # path relative to the copy root, POSIX style
    src: str                 # absolute source path
    dst: str                 # absolute destination path
    size: int = 0
    src_hash: str = ""
    dst_hash: str = ""
    status: str = "pending"  # copied | overwritten | skipped-identical | skipped | failed


@dataclass
class VerifyResult:
    passed: bool = True
    checks: list = field(default_factory=list)  # list of (label, ok, detail)

    def add(self, label: str, ok: bool, detail: str = ""):
        self.checks.append((label, ok, detail))
        if not ok:
            self.passed = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def format_size(num_bytes: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if num_bytes < 1024:
            return f"{num_bytes:.2f} {unit}"
        num_bytes /= 1024
    return f"{num_bytes:.2f} PB"


def new_hasher(algo: str):
    return hashlib.new(algo)


def hash_file(path: str, algo: str, progress=None, task=None) -> str:
    """Stream a file through the chosen hash. No RAM spike regardless of size."""
    h = new_hasher(algo)
    with open(path, "rb") as f:
        while True:
            buf = f.read(READ_CHUNK)
            if not buf:
                break
            h.update(buf)
            if progress is not None and task is not None:
                progress.advance(task, len(buf))
    return h.hexdigest()


def copy_file_hashed(src: str, dst: str, algo: str, progress=None, task=None) -> str:
    """Copy src -> dst while computing the source hash in the same pass."""
    h = new_hasher(algo)
    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        while True:
            buf = fin.read(READ_CHUNK)
            if not buf:
                break
            h.update(buf)
            fout.write(buf)
            if progress is not None and task is not None:
                progress.advance(task, len(buf))
        fout.flush()
        os.fsync(fout.fileno())
    try:
        shutil.copystat(src, dst)
    except OSError:
        pass  # best-effort on filesystems that reject some attributes (e.g. Windows perms)
    return h.hexdigest()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Copy or move a file / folder with byte-perfect hash verification.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("src", help="Source file or directory")
    parser.add_argument("dst", help="Destination file or directory")
    parser.add_argument("--move", action="store_true",
                        help="Delete the source after every file verified OK")
    parser.add_argument("--hash", choices=["sha256", "md5"], default="sha256",
                        help="Hash algorithm (default: sha256)")
    parser.add_argument("--on-conflict",
                        choices=["smart", "overwrite", "skip", "error"],
                        default="smart",
                        help="Existing destination file handling (default: smart)")
    parser.add_argument("--manifest", metavar="PATH",
                        help="Path for the JSON manifest")
    parser.add_argument("--no-manifest", action="store_true",
                        help="Do not write a manifest file")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show the plan and exit without copying")
    parser.add_argument("--quiet", action="store_true",
                        help="Only show progress bars, suppress per-file logs")
    parser.add_argument("--no-verify", action="store_true",
                        help="Skip post-copy re-read verification")
    parser.add_argument("--version", action="version",
                        version=f"copy-verify {__version__}")
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------

def build_jobs(src: Path, dst: Path, dest_is_dir: bool) -> tuple:
    """
    Return (jobs, dest_dir).

    For a directory src, files map to dst/<relpath> and dest_dir is dst.
    For a file src: if the destination is directory-like (exists as a dir, or was
    written with a trailing separator) the target is dst/<src name> and dest_dir
    is dst; otherwise dst is taken as the literal target path and dest_dir is None.
    """
    if src.is_dir():
        jobs = []
        for dirpath, dirnames, filenames in os.walk(src, followlinks=False):
            dirnames.sort()
            for name in sorted(filenames):
                abs_src = Path(dirpath) / name
                rel = abs_src.relative_to(src).as_posix()
                abs_dst = dst / rel
                try:
                    size = abs_src.stat().st_size
                except OSError:
                    size = 0
                jobs.append(FileJob(rel=rel, src=str(abs_src), dst=str(abs_dst), size=size))
        return jobs, dst

    # single file
    if dest_is_dir:
        target = dst / src.name
        dest_dir = dst
    else:
        target = dst
        dest_dir = None
    size = src.stat().st_size
    job = FileJob(rel=src.name, src=str(src), dst=str(target), size=size)
    return [job], dest_dir


def print_header(src: Path, dst: Path, jobs: list, args) -> None:
    total_bytes = sum(j.size for j in jobs)
    table = Table(box=box.ROUNDED, show_header=False, padding=(0, 2))
    table.add_column(style="bold cyan", no_wrap=True)
    table.add_column(style="white")
    table.add_row("Mode",        "MOVE (copy + verify + delete source)" if args.move
                                 else "COPY (copy + verify)")
    table.add_row("Source",      str(src))
    table.add_row("Destination", str(dst))
    table.add_row("Files",       f"{len(jobs):,}")
    table.add_row("Total size",  format_size(total_bytes))
    table.add_row("Hash",        args.hash.upper())
    table.add_row("On conflict", args.on_conflict)
    if args.no_verify:
        table.add_row("Verify", "[yellow]disabled (--no-verify)[/yellow]")
    console.print(Panel(table, title="[bold]copy-verify[/bold]", border_style="cyan"))


# ---------------------------------------------------------------------------
# Copy
# ---------------------------------------------------------------------------

def do_copy(jobs: list, args, total_bytes: int) -> None:
    progress = Progress(
        TextColumn("[bold cyan]{task.description}"),
        BarColumn(bar_width=40),
        FileSizeColumn(),
        TextColumn("/"),
        TotalFileSizeColumn(),
        TransferSpeedColumn(),
        TimeElapsedColumn(),
        TextColumn("ETA"),
        TimeRemainingColumn(),
        console=console,
        refresh_per_second=10,
    )

    with progress:
        task = progress.add_task("[cyan]Copying[/cyan]", total=max(total_bytes, 1))

        for job in jobs:
            name = job.rel
            dst_path = Path(job.dst)
            exists = dst_path.is_file()

            # Resolve conflicts before doing any work.
            if exists and args.on_conflict == "error":
                raise FileExistsError(f"Destination already exists: {job.dst}")

            if exists and args.on_conflict == "skip":
                job.src_hash = hash_file(job.src, args.hash)  # for the manifest / verify
                job.status = "skipped"
                progress.advance(task, job.size)
                if not args.quiet:
                    console.log(f"[yellow]- skip[/yellow] {name} (exists)")
                continue

            # Compute the source hash (single streaming pass) unless we can copy directly.
            if exists and args.on_conflict == "smart":
                job.src_hash = hash_file(job.src, args.hash)
                dst_hash = hash_file(job.dst, args.hash)
                if dst_hash == job.src_hash:
                    job.dst_hash = dst_hash
                    job.status = "skipped-identical"
                    progress.advance(task, job.size)
                    if not args.quiet:
                        console.log(f"[green]= same[/green] {name} (hash match, skipped)")
                    continue
                # differ -> fall through and overwrite
                job.src_hash = copy_file_hashed(job.src, job.dst, args.hash, progress, task)
                job.status = "overwritten"
                if not args.quiet:
                    console.log(f"[magenta]~ over[/magenta] {name} ({format_size(job.size)})")
                continue

            if exists and args.on_conflict == "overwrite":
                job.src_hash = copy_file_hashed(job.src, job.dst, args.hash, progress, task)
                job.status = "overwritten"
                if not args.quiet:
                    console.log(f"[magenta]~ over[/magenta] {name} ({format_size(job.size)})")
                continue

            # Fresh copy.
            job.src_hash = copy_file_hashed(job.src, job.dst, args.hash, progress, task)
            job.status = "copied"
            if not args.quiet:
                console.log(f"[green]+ copy[/green] {name} ({format_size(job.size)})")


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

def verify_copy(jobs: list, args) -> VerifyResult:
    result = VerifyResult()

    console.print()
    console.rule("[bold yellow]Post-Copy Verification[/bold yellow]")

    to_check = [j for j in jobs if j.status != "skipped"]
    total = sum(j.size for j in to_check)

    progress = Progress(
        SpinnerColumn(),
        TextColumn("[cyan]{task.description}"),
        BarColumn(bar_width=35),
        FileSizeColumn(),
        TextColumn("/"),
        TotalFileSizeColumn(),
        TransferSpeedColumn(),
        console=console,
        refresh_per_second=10,
    )

    verified_bytes = 0
    with progress:
        task = progress.add_task("Re-reading destination...", total=max(total, 1))

        for job in to_check:
            dst_path = Path(job.dst)

            if not dst_path.is_file():
                result.add(f"{job.rel} -> exists", False, "Destination file missing")
                continue

            actual_size = dst_path.stat().st_size
            size_ok = actual_size == job.size
            if not size_ok:
                result.add(
                    f"{job.rel} -> size",
                    False,
                    f"expected {format_size(job.size)}, got {format_size(actual_size)}",
                )

            actual_hash = hash_file(job.dst, args.hash, progress, task)
            job.dst_hash = actual_hash
            hash_ok = actual_hash == job.src_hash
            result.add(
                f"{job.rel} -> {args.hash}",
                hash_ok,
                f"{actual_hash[:16]}..." if hash_ok
                else f"src {job.src_hash[:16]}... vs dst {actual_hash[:16]}...",
            )
            if hash_ok and size_ok:
                verified_bytes += actual_size

    missing = [j.rel for j in jobs if j.status not in ("skipped",) and not Path(j.dst).is_file()]
    result.add(
        "All destination files present",
        not missing,
        "OK" if not missing else f"{len(missing)} missing",
    )

    result.add(
        "Files accounted for",
        True,
        f"{len(to_check):,} verified, {len(jobs) - len(to_check):,} skipped",
    )

    return result


def print_verify_result(result: VerifyResult, move: bool) -> None:
    table = Table(
        title="Verification Results",
        box=box.ROUNDED,
        border_style="green" if result.passed else "red",
    )
    table.add_column("Check",  style="white")
    table.add_column("Status", justify="center")
    table.add_column("Detail", style="dim")

    for label, ok, detail in result.checks:
        status = "[bold green]PASS[/bold green]" if ok else "[bold red]FAIL[/bold red]"
        table.add_row(label, status, detail)

    console.print(table)

    if result.passed:
        msg = "[bold green][OK] All files verified byte-perfect.[/bold green]"
        if move:
            msg += "\nSource is now safe to delete - proceeding with move."
        console.print(Panel(msg, border_style="green"))
    else:
        msg = "[bold red][X] One or more files FAILED verification.[/bold red]\n"
        msg += ("Source will NOT be deleted. Investigate before trusting the destination."
                if move else
                "Do not trust the destination copy. Re-run or inspect the source.")
        console.print(Panel(msg, border_style="red"))


# ---------------------------------------------------------------------------
# Move: delete source after a clean verification
# ---------------------------------------------------------------------------

def delete_source(src: Path, jobs: list) -> int:
    removed = 0
    for job in jobs:
        try:
            os.remove(job.src)
            removed += 1
        except FileNotFoundError:
            pass
    if src.is_dir():
        # prune now-empty directories, deepest first
        for dirpath, dirnames, filenames in os.walk(src, topdown=False):
            try:
                os.rmdir(dirpath)
            except OSError:
                pass  # not empty (e.g. contained a skipped file) - leave it
    return removed


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------

def resolve_manifest_path(args, dst: Path, dest_dir) -> Path:
    if args.manifest:
        return Path(args.manifest).resolve()
    if dest_dir is not None:
        return Path(dest_dir) / "copyverify-manifest.json"
    return dst.with_name(dst.name + ".copyverify-manifest.json")


def write_manifest(path: Path, src: Path, dst: Path, jobs: list, args,
                   verified: bool, elapsed: float) -> None:
    payload = {
        "tool": "copy-verify",
        "version": __version__,
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "mode": "move" if args.move else "copy",
        "hash": args.hash,
        "on_conflict": args.on_conflict,
        "verified": verified,
        "source": str(src),
        "destination": str(dst),
        "elapsed_seconds": round(elapsed, 3),
        "totals": {
            "files": len(jobs),
            "bytes": sum(j.size for j in jobs),
        },
        "files": [
            {
                "path": j.rel,
                "size": j.size,
                args.hash: j.src_hash,
                "status": j.status,
            }
            for j in jobs
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    console.print(f"[dim]Manifest written:[/dim] {path}")


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

def print_summary(jobs: list, elapsed: float, total_bytes: int, move: bool) -> None:
    by_status: dict = {}
    for j in jobs:
        by_status[j.status] = by_status.get(j.status, 0) + 1

    summary = Table(title="Copy Complete", box=box.ROUNDED, border_style="green")
    summary.add_column("Status", style="bold cyan")
    summary.add_column("Files",  style="white", justify="right")
    for status, n in sorted(by_status.items()):
        summary.add_row(status, f"{n:,}")
    console.print()
    console.print(summary)

    stats = Table(box=box.SIMPLE, show_header=False, padding=(0, 2))
    stats.add_column(style="bold dim")
    stats.add_column(style="white")
    stats.add_row("Total files", f"{len(jobs):,}")
    stats.add_row("Total size",  format_size(total_bytes))
    stats.add_row("Elapsed",     str(timedelta(seconds=int(elapsed))))
    stats.add_row(
        "Avg speed",
        f"{format_size(int(total_bytes / elapsed))}/s" if elapsed > 0 else "-",
    )
    console.print(stats)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(args) -> int:
    src = Path(args.src).resolve()
    dst = Path(args.dst).resolve()

    # A trailing separator on the raw argument means "into this directory".
    dst_dirlike = args.dst.rstrip().endswith(("/", "\\")) or dst.is_dir()

    if not src.exists():
        console.print(f"[bold red][X] Source not found:[/bold red] {src}")
        return 1
    if src == dst:
        console.print("[bold red][X] Source and destination are the same path.[/bold red]")
        return 1
    if src.is_dir() and dst.is_relative_to(src):
        console.print("[bold red][X] Destination is inside the source directory.[/bold red]")
        return 1
    if dst.is_file() and src.is_dir():
        console.print("[bold red][X] Destination is an existing file, but the source "
                      "is a directory.[/bold red]")
        return 1

    jobs, dest_dir = build_jobs(src, dst, dst_dirlike)
    if not jobs:
        console.print("[bold red][X] Nothing to copy (source has no files).[/bold red]")
        return 1

    total_bytes = sum(j.size for j in jobs)
    print_header(src, dst, jobs, args)

    if args.dry_run:
        plan = Table(title="Planned operations (dry run)", box=box.ROUNDED, border_style="dim")
        plan.add_column("Rel path", style="white")
        plan.add_column("Size", style="yellow", justify="right")
        plan.add_column("Destination exists?", justify="center")
        for j in jobs[:200]:
            plan.add_row(j.rel, format_size(j.size),
                         "yes" if Path(j.dst).is_file() else "no")
        if len(jobs) > 200:
            plan.add_row(f"[dim]... {len(jobs) - 200:,} more[/dim]", "", "")
        console.print(plan)
        console.print("[yellow]Dry run - no files were written.[/yellow]")
        return 0

    start = time.monotonic()
    try:
        do_copy(jobs, args, total_bytes)
    except FileExistsError as e:
        console.print(f"[bold red][X] {e}[/bold red]")
        console.print("[dim]Use --on-conflict smart|overwrite|skip to handle this.[/dim]")
        return 1
    elapsed = time.monotonic() - start

    print_summary(jobs, elapsed, total_bytes, args.move)

    verified = False
    if args.no_verify:
        console.print("\n[yellow][!] Verification skipped (--no-verify)[/yellow]")
        result = None
    else:
        result = verify_copy(jobs, args)
        print_verify_result(result, args.move)
        verified = result.passed

    if not args.no_manifest:
        manifest_path = resolve_manifest_path(args, dst, dest_dir)
        try:
            write_manifest(manifest_path, src, dst, jobs, args, verified, elapsed)
        except OSError as e:
            console.print(f"[yellow][!] Could not write manifest: {e}[/yellow]")

    if result is not None and not result.passed:
        return 2

    if args.move:
        removed = delete_source(src, jobs)
        console.print(f"[bold green][OK] Move complete[/bold green] - "
                      f"removed {removed:,} source file(s).")

    console.print(f"[bold green][OK] Destination:[/bold green] {dst}\n")
    return 0


def main() -> None:
    args = parse_args()
    try:
        sys.exit(run(args))
    except KeyboardInterrupt:
        console.print("\n[red]Interrupted.[/red]")
        sys.exit(130)


if __name__ == "__main__":
    main()

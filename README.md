# copy-verify

Copy or move a file or folder, then prove the result is **byte-perfect** with SHA-256 hashes.

Built for large transfers where a silent bad block, a truncated write, or a flaky
USB/network mount can corrupt data without any error. Streaming I/O keeps RAM usage
flat regardless of file size.

---

## Features

- Copy **or** move (`--move` deletes the source only after every file passes verification)
- Works on a single file or a whole directory tree (structure preserved)
- 3-phase integrity flow: **scan plan → copy + hash → re-read from disk & compare**
- Resume-friendly: `--on-conflict smart` skips files whose hash already matches
- JSON manifest with a hash for every file, reusable by the standalone verifier
- Standalone `verify_copy.py` — re-check any time, from a manifest or by comparing trees
- `--dry-run` to preview the plan
- Real-time progress bar with speed and ETA
- Cross-platform: Linux, macOS, Windows

---

## Requirements

- Python 3.10 or newer
- [`rich`](https://github.com/Textualize/rich) >= 13.0.0

---

## Installation

### Quick install (recommended)

The install script creates a private `.venv`, installs dependencies, runs a smoke
test, and adds `copyverify` / `verify-copy` launchers to your PATH.

**Linux / macOS**

```bash
git clone https://github.com/teerartms/copy-verify.git
cd copy-verify
./install.sh
```

**Windows (PowerShell)**

```powershell
git clone https://github.com/teerartms/copy-verify.git
cd copy-verify
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

Useful flags (both scripts): `--no-launchers` to skip the PATH change,
`--bin-dir DIR` / `-BinDir DIR` to choose where launchers go,
`--python PATH` / `-Python PATH` to pick a specific interpreter.
Open a new terminal afterwards so the PATH change takes effect.

### Manual install

<details>
<summary>Set up the virtual environment by hand</summary>

**Linux / macOS**

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

**Windows (PowerShell)**

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

> If PowerShell blocks the activate script:
> ```powershell
> Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
> ```

</details>

---

## Usage

### Copy / move

```
python copyverify.py <src> <dst> [options]
```

| Argument | Description |
|---|---|
| `src` | Source file or directory |
| `dst` | Destination file or directory |
| `--move` | Delete the source after every file is verified OK |
| `--hash {sha256,md5}` | Hash algorithm (default: `sha256`) |
| `--on-conflict {smart,overwrite,skip,error}` | Existing-destination handling (default: `smart`) |
| `--manifest PATH` | Where to write the JSON manifest |
| `--no-manifest` | Do not write a manifest |
| `--dry-run` | Show the plan and exit |
| `--quiet` | Suppress per-file logs |
| `--no-verify` | Skip the re-read verification (not recommended) |
| `--version` | Show version and exit |

**`--on-conflict` modes**

| Mode | Behaviour when the destination file already exists |
|---|---|
| `smart` (default) | Hash both sides — skip if identical, overwrite if different |
| `overwrite` | Always overwrite |
| `skip` | Never touch an existing file (its source hash is still recorded) |
| `error` | Abort the whole run |

**Examples**

```bash
# Copy a folder and verify every file
python copyverify.py ~/photos /mnt/backup/photos

# Move a large file to another disk; source is deleted only after verification passes
python copyverify.py bigfile.iso /mnt/archive/ --move

# Resume an interrupted copy — already-correct files are skipped
python copyverify.py ~/photos /mnt/backup/photos --on-conflict smart

# Preview only
python copyverify.py ~/photos /mnt/backup/photos --dry-run

# Faster, non-cryptographic corruption check
python copyverify.py ~/photos /mnt/backup/photos --hash md5
```

Output layout mirrors the source. A manifest is written to
`<dst>/copyverify-manifest.json` for a folder, or
`<dst>.copyverify-manifest.json` for a single file.

---

### Verify (standalone)

Re-check a destination at any later time.

```
# From the manifest produced during the copy
python verify_copy.py --manifest /mnt/backup/photos/copyverify-manifest.json /mnt/backup/photos

# Or by comparing the source and destination directly
python verify_copy.py ~/photos /mnt/backup/photos
```

| Argument | Description |
|---|---|
| `src` | Source file/dir (pair mode; omit when using `--manifest`) |
| `dst` | Destination file/dir |
| `--manifest PATH` | Manifest JSON from `copyverify.py` (manifest mode) |
| `--hash {sha256,md5}` | Hash algorithm for pair mode (default: `sha256`) |
| `--version` | Show version and exit |

---

## How verification works

`copyverify.py` runs three phases:

1. **Scan plan** — walk the source, stat every file, build the file list and byte total.
2. **Copy + hash** — stream each file to the destination, computing the source hash in
   the same pass. In `smart` mode an existing destination file is hashed first and
   skipped if it already matches.
3. **Re-read & compare** — read every destination file back *from disk*, recompute its
   hash, and compare it to the source hash. Also checks file size, that every expected
   file is present, and the total file/byte counts.

If any file fails, the script exits `2` and — in `--move` mode — **the source is left
untouched**.

`verify_copy.py` performs the same per-file and global checks independently, either
from the manifest or by hashing the source tree directly.

---

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Success (verified, or verification skipped) |
| `1` | Argument or file error |
| `2` | Verification failed |

---

## Notes and limits

- Symlinked directories are not followed (no infinite loops); symlinked files are
  copied as regular files.
- Empty directories are not recreated at the destination.
- File metadata (mtime, permissions) is copied best-effort via `shutil.copystat`;
  attributes a filesystem rejects are skipped silently.
- Verification proves **content** equality by hash — not ACLs, xattrs, or ADS.

---

## License

MIT — see [LICENSE](LICENSE) for details.

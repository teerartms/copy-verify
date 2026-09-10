#!/usr/bin/env bash
#
# install.sh - Set up copy-verify on Linux / macOS.
#
# Creates a local virtual environment in .venv, installs the dependencies,
# and drops `copyverify` / `verify-copy` launchers into a bin directory on
# your PATH (default: ~/.local/bin).
#
# Usage:
#     ./install.sh [--no-launchers] [--bin-dir DIR] [--python PYTHON]
#
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$REPO_DIR/.venv"
BIN_DIR="${HOME}/.local/bin"
PYTHON=""
MAKE_LAUNCHERS=1

while [ $# -gt 0 ]; do
    case "$1" in
        --no-launchers) MAKE_LAUNCHERS=0; shift ;;
        --bin-dir)      BIN_DIR="$2"; shift 2 ;;
        --python)       PYTHON="$2"; shift 2 ;;
        -h|--help)
            sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'
            exit 0 ;;
        *) echo "Unknown option: $1" >&2; exit 2 ;;
    esac
done

# --- locate a suitable Python -----------------------------------------------
# A candidate must actually run AND be >= 3.10 (skips the Windows Store stub,
# python2, etc.).
py_ok() {
    "$1" -c 'import sys; sys.exit(0 if sys.version_info[:2] >= (3, 10) else 1)' \
        >/dev/null 2>&1
}

if [ -n "$PYTHON" ]; then
    py_ok "$PYTHON" || { echo "Error: $PYTHON is not Python 3.10+." >&2; exit 1; }
else
    for cand in python3 python3.13 python3.12 python3.11 python3.10 python; do
        if command -v "$cand" >/dev/null 2>&1 && py_ok "$cand"; then
            PYTHON="$cand"; break
        fi
    done
fi
if [ -z "$PYTHON" ]; then
    echo "Error: no Python 3.10+ interpreter found. Install one and retry" >&2
    echo "       (or pass --python /path/to/python)." >&2
    exit 1
fi

PYV="$("$PYTHON" -c 'import sys; print(sys.version.split()[0])')"
echo "Using Python $PYV -> $(command -v "$PYTHON")"

# --- virtual environment ----------------------------------------------------
if [ ! -d "$VENV_DIR" ]; then
    echo "Creating virtual environment in $VENV_DIR"
    "$PYTHON" -m venv "$VENV_DIR"
else
    echo "Reusing existing virtual environment in $VENV_DIR"
fi

# POSIX venvs use bin/; a venv created on Windows (WSL/Git Bash) uses Scripts/.
VENV_PY="$VENV_DIR/bin/python"
[ -x "$VENV_PY" ] || VENV_PY="$VENV_DIR/Scripts/python.exe"
if [ ! -x "$VENV_PY" ]; then
    echo "Error: virtual environment Python not found under $VENV_DIR" >&2
    exit 1
fi
"$VENV_PY" -m pip install --upgrade pip >/dev/null
echo "Installing dependencies..."
"$VENV_PY" -m pip install -r "$REPO_DIR/requirements.txt"

# --- smoke test -------------------------------------------------------------
"$VENV_PY" "$REPO_DIR/copyverify.py" --version
"$VENV_PY" "$REPO_DIR/verify_copy.py" --version

# --- launchers ------------------------------------------------------------
if [ "$MAKE_LAUNCHERS" -eq 1 ]; then
    mkdir -p "$BIN_DIR"

    cat > "$BIN_DIR/copyverify" <<EOF
#!/usr/bin/env bash
exec "$VENV_PY" "$REPO_DIR/copyverify.py" "\$@"
EOF
    cat > "$BIN_DIR/verify-copy" <<EOF
#!/usr/bin/env bash
exec "$VENV_PY" "$REPO_DIR/verify_copy.py" "\$@"
EOF
    chmod +x "$BIN_DIR/copyverify" "$BIN_DIR/verify-copy"
    echo
    echo "Launchers installed:"
    echo "  $BIN_DIR/copyverify"
    echo "  $BIN_DIR/verify-copy"
    case ":$PATH:" in
        *":$BIN_DIR:"*) : ;;
        *) echo
           echo "NOTE: $BIN_DIR is not on your PATH. Add this to your shell rc:"
           echo "  export PATH=\"$BIN_DIR:\$PATH\"" ;;
    esac
fi

echo
echo "Done. Try:"
echo "  copyverify <src> <dst>"
echo "  verify-copy --manifest <dst>/copyverify-manifest.json <dst>"
echo
echo "Or without launchers:"
echo "  $VENV_PY $REPO_DIR/copyverify.py <src> <dst>"

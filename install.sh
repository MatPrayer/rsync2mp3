#!/usr/bin/env bash
# Install rsync2mp3, rsync2opus and rsync2aac into ~/.local/bin.
# Works on Linux and macOS; on macOS the default bash is 3.2, so nothing here
# may use associative arrays, `${x^^}` or an empty array under `set -u`.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
bindir="${XDG_BIN_HOME:-$HOME/.local/bin}"
mkdir -p "$bindir"

os="$(uname -s)"

for name in rsync2mp3 rsync2opus rsync2aac; do
    src="$here/$name.py"
    [ -f "$src" ] || continue
    ln -sf "$src" "$bindir/$name"
    chmod +x "$src"
    echo "installed: $bindir/$name -> $src"
done

# macOS does not put ~/.local/bin on PATH, and neither does a bare Linux login
# shell. Say so once rather than leaving a working install that looks broken.
case ":$PATH:" in
    *":$bindir:"*) ;;
    *)
        echo "note: $bindir is not on PATH. Add it:"
        if [ -n "${FISH_VERSION:-}" ] || [ "$(basename "${SHELL:-}")" = fish ]; then
            echo "  fish_add_path $bindir"
        else
            echo "  echo 'export PATH=\"$bindir:\$PATH\"' >> ~/.zshrc  # or ~/.bashrc"
        fi
        ;;
esac

python_bin="${PYTHON:-python3}"
if ! command -v "$python_bin" >/dev/null; then
    echo "missing dependency: python3"
    exit 1
fi
# tomllib landed in 3.11 and the scripts use it unconditionally.
if ! "$python_bin" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
    echo "$python_bin is $("$python_bin" -V 2>&1); these tools need Python 3.11 or newer"
    exit 1
fi

# Plain strings, not arrays: bash 3.2 treats an empty array as unset under -u.
missing=""
for tool in ffmpeg ffprobe; do
    command -v "$tool" >/dev/null || missing="$missing $tool"
done
"$python_bin" -c 'import mutagen' 2>/dev/null || missing="$missing mutagen"

if [ -n "$missing" ]; then
    echo "missing dependencies:$missing"
    case "$os" in
        Darwin)
            echo "  brew install ffmpeg"
            echo "  $python_bin -m pip install --user mutagen"
            echo "  (Homebrew's Python is externally managed: if pip refuses,"
            echo "   use 'pipx install mutagen' or add --break-system-packages)"
            ;;
        *)
            echo "  sudo pacman -S ffmpeg python-mutagen"
            ;;
    esac
    exit 1
fi

# The distro ffmpeg usually has them, but a minimal or self-built one may not,
# and the tools can do nothing without their encoder.
# Not piped into grep -q: that exits on the first match, SIGPIPEs ffmpeg, and
# `set -o pipefail` then reports the whole pipeline as failed.
encoders="$(ffmpeg -hide_banner -encoders 2>/dev/null || true)"
for pair in "libmp3lame:rsync2mp3" "libopus:rsync2opus" "aac:rsync2aac"; do
    enc="${pair%%:*}"
    tool="${pair##*:}"
    if [[ "$encoders" != *" $enc "* ]]; then
        echo "warning: this ffmpeg has no $enc encoder — $tool will fail"
    fi
done
# Not a warning: libfdk_aac is the better AAC encoder but its licence keeps it
# out of both distro and Homebrew builds, and rsync2aac falls back to the
# native one on its own.
if [[ "$encoders" == *libfdk_aac* ]]; then
    echo "note: libfdk_aac found — rsync2aac will use it by default"
fi

if [ "$os" = Darwin ]; then
    echo "note: config is read from ~/Library/Application Support/rsync2/config.toml"
    echo "      (~/.config/rsync2/config.toml still works too)"
fi

echo "all dependencies present. try: rsync2mp3 --help"

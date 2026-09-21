#!/usr/bin/env python3
"""
rsync2aac — mirror a lossless music library into AAC, incrementally.

Works like rsync: point it at a source and a destination, run it as often as
you like, and only what changed gets processed. Either endpoint may be local
or remote in rsync's own syntax:

    rsync2aac sync /music /music-aac
    rsync2aac sync nas:/srv/music /music-aac
    rsync2aac sync /music user@nas:/srv/music-aac

Lossless sources (flac, wav, alac, ape, wv, ...) are transcoded to AAC in an
.m4a container. Already-lossy sources (mp3, aac, opus, vorbis) are copied
verbatim — they are never re-encoded. Cover art, lyrics and playlists come
along for the ride.

AAC is the middle target: better than MP3 at the same bitrate, worse than Opus,
and the only one of the three that Apple hardware plays natively. Use it for
iPhones, iPods, CarPlay and older Sonos gear; anything that reads Opus should
get rsync2opus instead.

Freshness is decided by mtime, the way rsync does it: the destination carries
its source's timestamp, so a re-run picks up only new and changed files. There
is no state database to lose or corrupt.
"""

from __future__ import annotations

import argparse
import atexit
import json
import os
import random
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

__version__ = "1.0.0"

# --- what we do with each kind of file ---------------------------------------

# Lossless / oversized sources worth transcoding.
TRANSCODE_EXT = {
    ".flac", ".wav", ".wave", ".aiff", ".aif", ".aifc",
    ".ape", ".wv", ".tta", ".tak", ".shn",
    ".dsf", ".dff", ".w64", ".caf",
}

# Already lossy — transcoding these again would only throw away quality.
COPY_AUDIO_EXT = {".mp3", ".opus", ".ogg", ".oga", ".aac", ".mpc", ".wma"}

# Containers that may hold either ALAC (lossless) or AAC (lossy); probed per file.
AMBIGUOUS_EXT = {".m4a", ".m4b", ".mp4", ".m4p"}

AUDIO_EXT = TRANSCODE_EXT | COPY_AUDIO_EXT | AMBIGUOUS_EXT

# Non-audio files worth keeping alongside the music.
SIDECAR_EXT = {
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tif", ".tiff",
    ".lrc", ".cue", ".m3u", ".m3u8", ".pls", ".pdf",
}

# Filenames that look like front cover art, best first.
COVER_NAMES = ("cover", "folder", "front", "album", "albumart", "artwork", "thumb")
COVER_IMAGE_EXT = (".jpg", ".jpeg", ".png", ".webp")

MIME_BY_EXT = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp",
}

FAILURE_LOG = ".rsync2aac-failures.log"
TMP_SUFFIX = ".r2a-tmp"

# Every ffmpeg sftp:// read opens its own SSH connection, so wide concurrency
# trips OpenSSH's MaxStartups (10 by default) and the server starts refusing.
REMOTE_JOB_CAP = 8

# Set by the SIGINT handler so workers wind down instead of pressing on.
_stop = threading.Event()


class Fatal(Exception):
    """An error that should stop the run with a clean message."""


# --- configuration ------------------------------------------------------------

CONFIG_NAME = "config.toml"
CONFIG_DIR = "rsync2"

# rsync2mp3, rsync2opus and rsync2aac share one config file. Top-level keys
# apply to all three; the per-codec tables below override them, which is where
# anything codec-specific (dest, bitrate, vbr) belongs.
CONFIG_SECTION = "aac"
CONFIG_SECTIONS = ("mp3", "opus", "aac")

# Keys a config file may set, mapped to the argparse dest they override.
CONFIG_KEYS = {
    "source", "dest", "bitrate", "vbr", "encoder", "jobs", "port",
    "max_duration", "art", "tags", "delete", "tolerance", "prune_empty",
    "keep_long",
}

# Keys any of the three tools accepts. A top-level key from this set that this
# tool has no use for (a sibling's `encoder`, say) is ignored rather than
# rejected — the file is shared, so it will legitimately hold such keys.
CONFIG_KEYS_ALL = CONFIG_KEYS | {"compression_level"}

DURATION_UNITS = {"h": 3600, "m": 60, "s": 1}


def parse_duration(spec: str | int | float) -> float:
    """Accept 1500, '1500', '25m', '1h30m', '90s'."""
    if isinstance(spec, (int, float)):
        return float(spec)
    text = str(spec).strip().lower()
    if not text:
        return 0.0
    try:
        return float(text)  # bare seconds
    except ValueError:
        pass
    total, number = 0.0, ""
    for ch in text:
        if ch.isdigit() or ch == ".":
            number += ch
        elif ch in DURATION_UNITS:
            if not number:
                raise Fatal(f"bad duration: {spec!r}")
            total += float(number) * DURATION_UNITS[ch]
            number = ""
        else:
            raise Fatal(f"bad duration: {spec!r} (use e.g. 25m, 1h30m, 90s)")
    if number:
        raise Fatal(f"bad duration: {spec!r} (missing unit on {number!r})")
    return total


def format_duration(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def exempt_matcher(patterns: list[str] | None):
    """Build a predicate for paths the duration limit must not touch.

    A pattern matches either as a literal relative path or as a glob. The
    literal check comes first because real filenames are full of brackets,
    which glob syntax would otherwise read as character classes.
    """
    import fnmatch

    pats = [p.strip().lower() for p in (patterns or []) if p.strip()]
    if not pats:
        return lambda rel: False

    def matches(rel: str) -> bool:
        low = rel.lower()
        return any(p == low or fnmatch.fnmatch(low, p) for p in pats)

    return matches


def config_search_path(explicit: str | None) -> list[Path]:
    if explicit:
        return [Path(explicit).expanduser()]
    paths = [Path.cwd() / CONFIG_NAME]
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        paths.append(Path(xdg).expanduser() / CONFIG_DIR / CONFIG_NAME)
    if sys.platform == "darwin":
        # The native macOS location, but ~/.config is still searched after it:
        # a dotfile repo shared with a Linux box will put the config there.
        paths.append(Path.home() / "Library" / "Application Support"
                     / CONFIG_DIR / CONFIG_NAME)
    if not xdg:
        paths.append(Path("~/.config").expanduser() / CONFIG_DIR / CONFIG_NAME)
    return paths


def merge_config_sections(raw: dict, path: Path) -> dict:
    """Flatten the shared config into the settings this tool cares about.

    Top-level keys are the shared defaults; the [aac] table wins over them.
    A sibling's table is skipped entirely, and a top-level key that only a
    sibling understands is dropped rather than rejected.
    """
    shared = {k: v for k, v in raw.items() if k not in CONFIG_SECTIONS}
    mine = raw.get(CONFIG_SECTION, {})
    if not isinstance(mine, dict):
        raise Fatal(f"{path}: [{CONFIG_SECTION}] must be a table")

    unknown = set(shared) - CONFIG_KEYS_ALL
    if unknown:
        raise Fatal(
            f"{path}: unknown setting(s) {', '.join(sorted(unknown))}\n"
            f"valid keys: {', '.join(sorted(CONFIG_KEYS))}"
        )
    unknown = set(mine) - CONFIG_KEYS
    if unknown:
        raise Fatal(
            f"{path}: unknown setting(s) in [{CONFIG_SECTION}]: "
            f"{', '.join(sorted(unknown))}\n"
            f"valid keys: {', '.join(sorted(CONFIG_KEYS))}"
        )

    settings = {k: v for k, v in shared.items() if k in CONFIG_KEYS}
    settings.update(mine)
    return settings


def load_config(explicit: str | None) -> tuple[dict, Path | None]:
    """Read the first config file that exists. CLI flags override it."""
    import tomllib

    for path in config_search_path(explicit):
        if not path.is_file():
            continue
        try:
            with path.open("rb") as fh:
                raw = tomllib.load(fh)
        except (OSError, tomllib.TOMLDecodeError) as e:
            raise Fatal(f"cannot read config {path}: {e}")

        raw = merge_config_sections(raw, path)
        if "max_duration" in raw:
            raw["max_duration"] = parse_duration(raw["max_duration"])
        # The CLI spells these as --no-art / --no-tags, so invert the friendlier
        # config names.
        if "art" in raw:
            raw["no_art"] = not raw.pop("art")
        if "tags" in raw:
            raw["no_tags"] = not raw.pop("tags")
        return raw, path

    if explicit:
        raise Fatal(f"config file not found: {explicit}")
    return {}, None


# --- small helpers ------------------------------------------------------------

def run(cmd: list[str], timeout: int = 3600, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, stdin=stdin, stdout=stdout,
                          stderr=subprocess.PIPE, timeout=timeout)


def human(n: float) -> str:
    for unit in ("B", "K", "M", "G", "T"):
        if abs(n) < 1024:
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}P"


def fresh(src_mtime: float, dst_mtime: float | None) -> bool:
    """Destination is current when it carries the source's timestamp.

    The 2s slack absorbs FAT/exFAT timestamp granularity, which matters for
    USB drives formatted for cross-platform use.
    """
    return dst_mtime is not None and abs(src_mtime - dst_mtime) <= 2


# --- endpoints: local and remote ----------------------------------------------

@dataclass
class Entry:
    size: int
    mtime: float


def fold(name: str) -> str:
    """Normalise a relative path for comparison between two filesystems.

    macOS stores filenames decomposed (NFD: "e" + combining acute) while Linux
    stores whatever bytes it was handed, usually composed (NFC). The same album
    therefore reads as two different names across the two, which would make
    every accented track look missing on one side and orphaned on the other.
    Only comparisons are folded; the stored names are left alone, because a
    delete or a stat has to use the name the filesystem really has.
    """
    return unicodedata.normalize("NFC", name)


class Listing(dict):
    """A {relative path: Entry} map whose lookups ignore NFC/NFD differences.

    Iteration, keys and `items()` stay byte-exact. Only `get`, `in` and the
    extra `real()` go through `fold`.
    """

    def _folded(self) -> dict[str, str]:
        index = getattr(self, "_index", None)
        if index is None or len(index) != len(self):
            index = self._index = {fold(k): k for k in self}
        return index

    def real(self, key: str) -> str:
        """The name as stored, for a key that may differ in normalisation."""
        if dict.__contains__(self, key):
            return key
        return self._folded().get(fold(key), key)

    def get(self, key, default=None):  # type: ignore[override]
        return dict.get(self, self.real(key), default)

    def __contains__(self, key) -> bool:  # type: ignore[override]
        return dict.__contains__(self, self.real(key))


class Endpoint:
    """A source or destination tree, local or over SSH."""

    is_remote = False

    def describe(self) -> str:
        raise NotImplementedError

    def listing(self) -> dict[str, Entry]:
        """Map of relative path -> Entry for every regular file in the tree."""
        raise NotImplementedError

    def fetch(self, rel: str, local: Path) -> None:
        """Copy rel out of this tree to a local path."""
        raise NotImplementedError

    def store(self, local: Path, rel: str, mtime: float) -> None:
        """Copy a local file into this tree at rel, stamped with mtime."""
        raise NotImplementedError

    def ffmpeg_url(self, rel: str) -> str:
        """A URL ffmpeg can open directly for reading."""
        raise NotImplementedError

    def unlink_many(self, rels: list[str]) -> list[str]:
        """Delete files; returns the ones that could not be removed."""
        raise NotImplementedError

    def prune_empty_dirs(self) -> int:
        raise NotImplementedError

    def restamp(self, stamps: list[tuple[str, float]]) -> list[str]:
        """Set mtimes on existing files; returns the ones that could not be set."""
        raise NotImplementedError

    def exists(self) -> bool:
        raise NotImplementedError

    def ensure(self) -> None:
        """Create the tree if it isn't there."""
        raise NotImplementedError


class LocalEndpoint(Endpoint):
    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve()

    def describe(self) -> str:
        return str(self.root)

    def path(self, rel: str) -> Path:
        return self.root / rel

    def exists(self) -> bool:
        return self.root.is_dir()

    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    def listing(self) -> Listing:
        out = Listing()
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
            here = Path(dirpath)
            for name in filenames:
                if name.startswith(".") or name.endswith(TMP_SUFFIX):
                    continue
                p = here / name
                try:
                    st = p.stat()
                except OSError:
                    continue
                if not os.path.isfile(p):
                    continue
                out[str(p.relative_to(self.root))] = Entry(st.st_size, st.st_mtime)
        return out

    def fetch(self, rel: str, local: Path) -> None:
        shutil.copyfile(self.path(rel), local)

    def store(self, local: Path, rel: str, mtime: float) -> None:
        dst = self.path(rel)
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(dst.name + TMP_SUFFIX)
        try:
            try:
                # Usually the same filesystem, so this is a cheap rename.
                os.replace(local, tmp)
            except OSError:
                shutil.copyfile(local, tmp)
            os.utime(tmp, (mtime, mtime))
            os.replace(tmp, dst)
        finally:
            tmp.unlink(missing_ok=True)

    def ffmpeg_url(self, rel: str) -> str:
        return str(self.path(rel))

    def unlink_many(self, rels: list[str]) -> list[str]:
        failed = []
        for rel in rels:
            try:
                self.path(rel).unlink()
            except OSError:
                failed.append(rel)
        return failed

    def restamp(self, stamps: list[tuple[str, float]]) -> list[str]:
        failed = []
        for rel, mtime in stamps:
            try:
                os.utime(self.path(rel), (mtime, mtime))
            except OSError:
                failed.append(rel)
        return failed

    def prune_empty_dirs(self) -> int:
        removed = 0
        for dirpath, _, _ in os.walk(self.root, topdown=False):
            d = Path(dirpath)
            if d == self.root:
                continue
            try:
                if not any(d.iterdir()):
                    d.rmdir()
                    removed += 1
            except OSError:
                pass
        return removed


class RemoteEndpoint(Endpoint):
    """An SSH endpoint, addressed rsync-style as [user@]host:/path."""

    is_remote = True
    _control_dir: str | None = None
    _control_lock = threading.Lock()
    _uname: str | None = None

    def __init__(self, host: str, root: str, port: int | None = None) -> None:
        self.host = host
        self.root = root.rstrip("/") or "/"
        self.port = port
        self._resolved = False

    def _resolve_root(self) -> None:
        """Turn a home-relative remote path into an absolute one.

        ssh commands run from the login directory, so `media/music` works for
        them, but sftp:// URLs are absolute from /. Without this, every read
        would fail with "No such file".
        """
        if self._resolved or self.root.startswith("/"):
            self._resolved = True
            return
        r = self.ssh(f"cd {shlex.quote(self.root)} && pwd")
        out = r.stdout.decode(errors="replace").strip()
        if r.returncode == 0 and out.startswith("/"):
            self.root = out
        self._resolved = True

    def describe(self) -> str:
        return f"{self.host}:{self.root}"

    def is_bsd(self) -> bool:
        """Whether the remote ships BSD userland rather than GNU coreutils.

        macOS and the BSDs have no `find -printf` and no `touch -d @EPOCH`, so
        the listing and timestamp commands need a different spelling there. One
        round trip, cached for the run.
        """
        if self._uname is None:
            r = self.ssh("uname -s")
            out = r.stdout.decode(errors="replace").strip()
            self._uname = out if r.returncode == 0 and out else "Linux"
        return self._uname in ("Darwin", "FreeBSD", "OpenBSD", "NetBSD",
                               "DragonFly")

    def path(self, rel: str) -> str:
        return str(PurePosixPath(self.root) / rel)

    # SSH connection multiplexing: one TCP+auth handshake for the whole run
    # instead of one per file. With thousands of files this is the difference
    # between minutes and hours.
    @classmethod
    def _control_opts(cls) -> list[str]:
        with cls._control_lock:
            if cls._control_dir is None:
                # A control socket is a Unix socket, so its path has to fit in
                # ~104 bytes. macOS points TMPDIR at a long /var/folders/...
                # path, which blows that budget on its own, hence the explicit
                # /tmp there; %C is a fixed-length hash of user+host+port, so
                # the name cannot grow with a long hostname either.
                base = "/tmp" if sys.platform == "darwin" else None
                cls._control_dir = tempfile.mkdtemp(prefix="r2aac-ssh-", dir=base)
                atexit.register(shutil.rmtree, cls._control_dir, ignore_errors=True)
        return [
            "-o", "ControlMaster=auto",
            "-o", f"ControlPath={cls._control_dir}/%C",
            "-o", "ControlPersist=120",
            "-o", "BatchMode=yes",
        ]

    def ssh_cmd(self, remote_command: str) -> list[str]:
        cmd = ["ssh", *self._control_opts()]
        if self.port:
            cmd += ["-p", str(self.port)]
        return [*cmd, self.host, remote_command]

    def ssh(self, remote_command: str, **kw) -> subprocess.CompletedProcess:
        return run(self.ssh_cmd(remote_command), **kw)

    def exists(self) -> bool:
        r = self.ssh(f"test -d {shlex.quote(self.root)}")
        if r.returncode not in (0, 1):
            raise Fatal(
                f"ssh to {self.host} failed: "
                f"{r.stderr.decode(errors='replace').strip() or 'unknown error'}"
            )
        if r.returncode == 0:
            self._resolve_root()
            return True
        return False

    def ensure(self) -> None:
        r = self.ssh(f"mkdir -p {shlex.quote(self.root)}")
        if r.returncode != 0:
            raise Fatal(f"cannot create {self.describe()}: "
                        f"{r.stderr.decode(errors='replace').strip()}")
        self._resolve_root()

    def listing(self) -> Listing:
        # One round trip for the whole tree.
        bsd = self.is_bsd()
        if bsd:
            # BSD find has no -printf, so stat does the formatting. Its output
            # is newline-separated (there is no NUL escape in a stat format),
            # which is why a filename containing a newline is dropped below
            # instead of being silently mangled.
            script = (
                f"cd {shlex.quote(self.root)} && "
                "find . -type f -exec stat -f '%z%t%m%t%N' {} +"
            )
        else:
            # NUL-separated, so filenames containing newlines survive intact.
            script = (
                f"cd {shlex.quote(self.root)} && "
                "find . -type f -printf '%s\\t%T@\\t%P\\0'"
            )
        r = self.ssh(script, timeout=1800)
        if r.returncode != 0:
            raise Fatal(
                f"listing {self.describe()} failed: "
                f"{r.stderr.decode(errors='replace').strip()}\n"
                "(rsync2aac needs find, and stat on a BSD remote)"
            )
        out = Listing()
        for record in r.stdout.split(b"\n" if bsd else b"\0"):
            record = record.strip(b"\r") if bsd else record
            if not record:
                continue
            try:
                size, mtime, rel = record.split(b"\t", 2)
                name = rel.decode("utf-8", errors="surrogateescape")
            except ValueError:
                continue
            if bsd:
                # stat -f %N echoes the path as find passed it: "./sub/file".
                name = name[2:] if name.startswith("./") else name
                if not name:
                    continue
            base = name.rsplit("/", 1)[-1]
            if base.startswith(".") or base.endswith(TMP_SUFFIX):
                continue
            out[name] = Entry(int(size), float(mtime))
        return out

    def fetch(self, rel: str, local: Path) -> None:
        for attempt in range(4):
            with local.open("wb") as fh:
                r = run(self.ssh_cmd(f"cat -- {shlex.quote(self.path(rel))}"),
                        stdout=fh)
            if r.returncode == 0 or not is_transient(r.stderr) or attempt == 3:
                break
            time.sleep(2 ** attempt + random.random())
        if r.returncode != 0:
            raise RuntimeError(
                f"fetch failed: {r.stderr.decode(errors='replace').strip()}")

    def store(self, local: Path, rel: str, mtime: float) -> None:
        target = self.path(rel)
        q = shlex.quote(target)
        qtmp = shlex.quote(target + TMP_SUFFIX)
        qdir = shlex.quote(str(PurePosixPath(target).parent))
        script = (
            f"mkdir -p {qdir} && cat > {qtmp} && "
            f"{touch_cmd(qtmp, mtime, self.is_bsd())} && mv -f {qtmp} {q}"
        )
        with local.open("rb") as fh:
            r = run(self.ssh_cmd(script), stdin=fh)
        if r.returncode != 0:
            raise RuntimeError(
                f"upload failed: {r.stderr.decode(errors='replace').strip()}")

    def ffmpeg_url(self, rel: str) -> str:
        port = f":{self.port}" if self.port else ""
        return f"sftp://{self.host}{port}{self.path(rel)}"

    def unlink_many(self, rels: list[str]) -> list[str]:
        # Batched so that deleting thousands of files is a handful of round
        # trips rather than thousands.
        failed: list[str] = []
        batch: list[str] = []
        size = 0
        for rel in rels + [None]:  # sentinel flushes the tail
            if rel is not None:
                q = shlex.quote(self.path(rel))
                batch.append(q)
                size += len(q) + 1
                if size < 100_000:
                    continue
            if batch:
                r = self.ssh("rm -f -- " + " ".join(batch))
                if r.returncode != 0:
                    failed.extend(batch)
            batch, size = [], 0
        return failed

    def restamp(self, stamps: list[tuple[str, float]]) -> list[str]:
        # Grouped by timestamp so one `touch` covers every file sharing it,
        # then batched like unlink_many to keep the round trips down.
        failed: list[str] = []
        by_time: dict[int, list[str]] = {}
        for rel, mtime in stamps:
            by_time.setdefault(int(mtime), []).append(rel)
        bsd = self.is_bsd()
        for when, rels in by_time.items():
            batch: list[str] = []
            size = 0
            for rel in rels + [None]:  # sentinel flushes the tail
                if rel is not None:
                    q = shlex.quote(self.path(rel))
                    batch.append(q)
                    size += len(q) + 1
                    if size < 100_000:
                        continue
                if batch:
                    r = self.ssh(touch_cmd(" ".join(batch), when, bsd,
                                           missing_ok=True))
                    if r.returncode != 0:
                        failed.extend(batch)
                batch, size = [], 0
        return failed

    def prune_empty_dirs(self) -> int:
        q = shlex.quote(self.root)
        # -print -delete rather than GNU's -printf, and `! -path` rather than
        # -mindepth: BSD find has neither of those two GNU spellings. -delete
        # implies -depth, so a directory emptied by this same pass goes too,
        # and the root is visited last, where `! -path` keeps it.
        r = self.ssh(
            f"find {q} ! -path {q} -type d -empty -print -delete | wc -l")
        if r.returncode != 0:
            return 0
        try:
            return int(r.stdout.strip())
        except ValueError:
            return 0


def touch_cmd(paths: str, mtime: float, bsd: bool, missing_ok: bool = False) -> str:
    """A shell command stamping already-quoted paths with an epoch time.

    BSD touch has no `-d @SECONDS`; its `-t` form reads the stamp in the local
    timezone, so the remote TZ is pinned to UTC to keep the two agreeing.
    """
    flags = "-c " if missing_ok else ""
    if bsd:
        stamp = time.strftime("%Y%m%d%H%M.%S", time.gmtime(mtime))
        return f"TZ=UTC0 touch {flags}-t {stamp} -- {paths}"
    return f"touch {flags}-d @{mtime:.0f} -- {paths}"


def parse_endpoint(spec: str, port: int | None) -> Endpoint:
    """Split an rsync-style endpoint into local or remote.

    A colon means remote, unless it is a Windows-style drive letter or the
    path simply exists locally (paths with colons in them are legal).
    """
    if os.path.isdir(spec) or spec.startswith((".", "/", "~")):
        return LocalEndpoint(Path(spec))
    head, sep, tail = spec.partition(":")
    if sep and head and "/" not in head:
        return RemoteEndpoint(head, tail or ".", port)
    return LocalEndpoint(Path(spec))


# --- media probing ------------------------------------------------------------

def probe(url: str) -> dict | None:
    r = run(["ffprobe", "-v", "error", "-show_streams", "-show_format",
             "-of", "json", url], timeout=180)
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return None


def audio_stream(info: dict) -> dict | None:
    for s in info.get("streams", ()):
        if s.get("codec_type") == "audio":
            return s
    return None


def duration_of(info: dict) -> float | None:
    for src in (info.get("format", {}), audio_stream(info) or {}):
        try:
            return float(src["duration"])
        except (KeyError, TypeError, ValueError):
            continue
    return None


# --- planning -----------------------------------------------------------------

@dataclass
class Job:
    rel: str            # path relative to the source root
    dst_rel: str        # path relative to the destination root
    action: str         # "transcode" | "copy"
    size: int
    mtime: float
    audio: bool = True  # sidecars have no duration to filter on


def with_suffix(rel: str, suffix: str) -> str:
    p = PurePosixPath(rel)
    return str(p.with_suffix(suffix))


def cover_candidates(listing: dict[str, Entry]) -> dict[str, str]:
    """Pick the best front-cover image in each directory, once for the tree."""
    best: dict[str, tuple[tuple[int, int], str]] = {}
    for rel in listing:
        p = PurePosixPath(rel)
        if p.suffix.lower() not in COVER_IMAGE_EXT:
            continue
        stem = p.stem.lower()
        rank = (2, 0)
        for i, name in enumerate(COVER_NAMES):
            if stem == name:
                rank = (0, i)
                break
            if name in stem:
                rank = (1, i)
                break
        if rank[0] == 2:
            continue
        d = str(p.parent)
        if d not in best or rank < best[d][0]:
            best[d] = (rank, rel)
    return {d: rel for d, (_, rel) in best.items()}


def probe_duration(url: str) -> float | None:
    """Duration only — much cheaper than a full stream dump."""
    r = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", url], timeout=120)
    if r.returncode != 0:
        return None
    try:
        return float(r.stdout.decode().strip())
    except ValueError:
        return None


def drop_long(jobs: list[Job], source: Endpoint, limit: float, workers: int,
              keep_long: list[str] | None = None
              ) -> tuple[list[Job], list[tuple[str, float]]]:
    """Remove audio longer than `limit` seconds.

    Every candidate has to be probed, which is why this only runs when a limit
    is actually configured — the default costs nothing.
    """
    exempt = exempt_matcher(keep_long)
    audio = [j for j in jobs if j.audio and not exempt(j.rel)]
    if not audio:
        return jobs, []

    print(f"probing durations of {len(audio)} audio files "
          f"(max_duration is set) ...", file=sys.stderr)
    progress = Progress(len(audio), label="probe ")
    too_long: dict[str, float] = {}
    lock = threading.Lock()

    def work(job: Job) -> None:
        if _stop.is_set():
            return
        d = probe_duration(source.ffmpeg_url(job.rel))
        # An unreadable duration is not grounds for skipping: let the file
        # through and let the encode surface any real problem.
        if d is not None and d > limit:
            with lock:
                too_long[job.rel] = d
        progress.tick()

    pmap(work, audio, workers)
    progress.finish()

    kept = [j for j in jobs if j.rel not in too_long]
    dropped = sorted(too_long.items(), key=lambda kv: -kv[1])
    return kept, dropped


def resolve_collisions(jobs: list[Job]) -> list[Job]:
    """Keep one job per destination path.

    Unlike the Opus target, .m4a is both an output container and a common input
    one, so a directory holding `track.flac` beside an AAC `track.m4a` produces
    two jobs writing the same destination — they would overwrite each other in
    whichever order the pool happened to run them. The transcode of the
    lossless master wins; the lossy twin is dropped.
    """
    chosen: dict[str, Job] = {}
    losers: list[tuple[str, str]] = []
    for j in jobs:
        prev = chosen.get(j.dst_rel)
        if prev is None:
            chosen[j.dst_rel] = j
        elif prev.action != "transcode" and j.action == "transcode":
            chosen[j.dst_rel] = j
            losers.append((prev.rel, j.rel))
        else:
            losers.append((j.rel, prev.rel))

    if losers:
        print(f"{len(losers)} sources collide on a destination path and were "
              f"skipped:", file=sys.stderr)
        for dropped, kept in losers[:10]:
            print(f"  {dropped}  (kept {kept})", file=sys.stderr)
        if len(losers) > 10:
            print(f"  ... and {len(losers) - 10} more", file=sys.stderr)
    return list(chosen.values())


def plan(src_listing: dict[str, Entry], source: Endpoint,
         dst_listing: dict[str, Entry], force: bool,
         max_duration: float = 0, workers: int = 8,
         keep_long: list[str] | None = None,
         adopt: list[Job] | None = None
         ) -> tuple[list[Job], list[Job], int, list[tuple[str, float]]]:
    """Classify every source file and work out what still needs doing."""
    jobs: list[Job] = []
    ignored = 0
    ambiguous: list[tuple[str, Entry]] = []

    for rel, e in sorted(src_listing.items()):
        ext = PurePosixPath(rel).suffix.lower()
        if ext in TRANSCODE_EXT:
            jobs.append(Job(rel, with_suffix(rel, ".m4a"), "transcode", e.size, e.mtime))
        elif ext in COPY_AUDIO_EXT:
            jobs.append(Job(rel, rel, "copy", e.size, e.mtime))
        elif ext in SIDECAR_EXT:
            jobs.append(Job(rel, rel, "copy", e.size, e.mtime, audio=False))
        elif ext in AMBIGUOUS_EXT:
            ambiguous.append((rel, e))
        else:
            ignored += 1

    # .m4a can be ALAC or AAC. Probing costs a file open, so only do it when
    # neither possible destination is already current.
    for rel, e in ambiguous:
        aac_rel = with_suffix(rel, ".m4a")
        if not force:
            done = dst_listing.get(aac_rel)
            if done is not None and fresh(e.mtime, done.mtime):
                # With an .m4a target the two destinations can be the same path,
                # so the path alone no longer says which way the file went. Byte
                # count does: a copy is exact, a transcode of ALAC never is.
                copied = aac_rel == rel and done.size == e.size
                jobs.append(Job(rel, aac_rel, "copy" if copied else "transcode",
                                e.size, e.mtime))
                continue
            done = dst_listing.get(rel)
            if aac_rel != rel and done is not None and fresh(e.mtime, done.mtime):
                jobs.append(Job(rel, rel, "copy", e.size, e.mtime))
                continue
        info = probe(source.ffmpeg_url(rel))
        st = audio_stream(info) if info else None
        lossless = bool(st and st.get("codec_name", "").startswith(("alac", "pcm")))
        jobs.append(Job(rel, aac_rel if lossless else rel,
                        "transcode" if lossless else "copy", e.size, e.mtime))

    jobs = resolve_collisions(jobs)

    # Reuse the destination's own spelling of a name that only differs by
    # Unicode normalisation, so a restamp or an overwrite lands on the file
    # that is already there instead of creating a second copy beside it.
    if isinstance(dst_listing, Listing):
        for j in jobs:
            j.dst_rel = dst_listing.real(j.dst_rel)

    pending = [
        j for j in jobs
        if force or not fresh(j.mtime, getattr(dst_listing.get(j.dst_rel), "mtime", None))
    ]

    # A source tree copied without -t comes back with every mtime rewritten, so
    # the whole library reads as changed. `--adopt` takes the destination at its
    # word instead: anything already there keeps its bytes and only gets the new
    # timestamp. Copies must still match byte count, since for those we can tell.
    if adopt is not None:
        keep = []
        for j in pending:
            e = dst_listing.get(j.dst_rel)
            if e is None or e.size == 0 or (j.action == "copy" and e.size != j.size):
                keep.append(j)
            else:
                adopt.append(j)
        pending = keep

    # Only new and changed files get probed. Probing the whole library on every
    # run would cost one ffprobe per track — minutes over SSH, for a result
    # that cannot have changed. Files already synced under a different limit
    # are `prune`'s job, not sync's.
    dropped: list[tuple[str, float]] = []
    if max_duration > 0 and pending:
        pending, dropped = drop_long(pending, source, max_duration, workers, keep_long)
        gone = {rel for rel, _ in dropped}
        jobs = [j for j in jobs if j.rel not in gone]
    return jobs, pending, ignored, dropped


# --- cover art ----------------------------------------------------------------

def extract_embedded_cover(url: str, tmpdir: Path,
                           info: dict | None = None) -> tuple[bytes, str, int, int] | None:
    info = info or probe(url)
    if not info:
        return None
    for s in info.get("streams", ()):
        if s.get("codec_type") != "video":
            continue
        if not s.get("disposition", {}).get("attached_pic"):
            continue
        ext = ".png" if s.get("codec_name") == "png" else ".jpg"
        out = tmpdir / f"cover{ext}"
        r = run(["ffmpeg", "-v", "error", "-y", "-i", url,
                 "-map", f"0:{s['index']}", "-c", "copy", str(out)], timeout=180)
        if r.returncode != 0 or not out.exists() or out.stat().st_size == 0:
            return None
        return (out.read_bytes(), MIME_BY_EXT.get(ext, "image/jpeg"),
                int(s.get("width") or 0), int(s.get("height") or 0))
    return None


# Magic bytes, because a surprising number of rips declare the wrong MIME type
# in their embedded picture blocks and players choke on the mismatch.
IMAGE_MAGIC = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"BM", "image/bmp"),
)


def sniff_mime(data: bytes, declared: str) -> str:
    """Trust the bytes, not the label."""
    for magic, mime in IMAGE_MAGIC:
        if data.startswith(magic):
            return mime
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return declared


# Tags the MP4 muxer already has an atom for. Everything else ffmpeg silently
# drops on the way into .m4a — which is most of what a well-tagged FLAC library
# carries: ReplayGain, label, catalogue number, ISRC, MusicBrainz ids, the
# track and disc totals. Those go into freeform atoms instead.
MP4_NATIVE_TAGS = {
    "title", "artist", "album", "album_artist", "composer", "comment",
    "genre", "date", "year", "track", "disc", "encoder", "copyright",
    "description", "grouping", "lyrics", "performer", "sort_album",
    "sort_album_artist", "sort_artist", "sort_composer", "sort_name",
    "show", "episode_id", "network", "media_type", "compilation",
}

# The namespace iTunes, beets and every ReplayGain-aware player look in.
FREEFORM_NS = "com.apple.iTunes"


def write_freeform_tags(m4a_path: Path, src_tags: dict) -> None:
    """Carry tags the MP4 muxer has no atom for into `----` freeform atoms."""
    from mutagen.mp4 import MP4, MP4FreeForm

    extra = {k: v for k, v in src_tags.items()
             if k.lower() not in MP4_NATIVE_TAGS and isinstance(v, str) and v}
    if not extra:
        return

    tags = MP4(str(m4a_path))
    if tags.tags is None:
        tags.add_tags()
    for key, value in extra.items():
        atom = f"----:{FREEFORM_NS}:{key.upper()}"
        tags[atom] = [MP4FreeForm(value.encode("utf-8"))]
    tags.save()


def to_jpeg(data: bytes) -> bytes | None:
    """Re-encode an image to JPEG. Returns None if ffmpeg cannot read it."""
    with tempfile.TemporaryDirectory(prefix="r2a-art-") as td:
        src = Path(td) / "in"
        out = Path(td) / "out.jpg"
        src.write_bytes(data)
        r = run(["ffmpeg", "-v", "error", "-y", "-i", str(src),
                 "-frames:v", "1", str(out)], timeout=60)
        if r.returncode == 0 and out.exists() and out.stat().st_size:
            return out.read_bytes()
    return None


def embed_cover(m4a_path: Path, cover: tuple[bytes, str, int, int]) -> None:
    """Write cover art into an .m4a as the MP4 `covr` atom.

    Done in a second pass rather than by ffmpeg, so that art coming from a
    folder image and art lifted out of the source file take the same path.
    """
    from mutagen.mp4 import MP4, MP4Cover

    data, mime, _width, _height = cover
    mime = sniff_mime(data, mime)
    # MP4 `covr` carries JPEG or PNG and nothing else. A webp or bmp folder
    # image is common enough to be worth converting rather than dropping.
    if mime not in ("image/jpeg", "image/png"):
        data = to_jpeg(data) or b""
        if not data:
            raise RuntimeError(f"cannot convert {mime} cover to jpeg")
        mime = "image/jpeg"

    tags = MP4(str(m4a_path))
    if tags.tags is None:
        tags.add_tags()
    fmt = MP4Cover.FORMAT_PNG if mime == "image/png" else MP4Cover.FORMAT_JPEG
    # Single cover, replacing whatever was there: players pick unpredictably
    # between duplicates.
    tags["covr"] = [MP4Cover(data, imageformat=fmt)]
    tags.save()


# --- the work -----------------------------------------------------------------

@dataclass
class Context:
    source: Endpoint
    dest: Endpoint
    bitrate: str
    art: bool
    vbr: int | None = None
    encoder: str = "aac"
    tags: bool = True
    covers: dict[str, str] = field(default_factory=dict)
    cover_cache: dict[str, tuple[bytes, str, int, int] | None] = field(default_factory=dict)
    cover_lock: threading.Lock = field(default_factory=threading.Lock)

    def folder_cover(self, rel: str) -> tuple[bytes, str, int, int] | None:
        """Fetch (once) the front-cover image sitting in this file's directory."""
        d = str(PurePosixPath(rel).parent)
        with self.cover_lock:
            if d in self.cover_cache:
                return self.cover_cache[d]
        cover_rel = self.covers.get(d)
        result = None
        if cover_rel:
            try:
                with tempfile.TemporaryDirectory(prefix="r2a-art-") as td:
                    local = Path(td) / PurePosixPath(cover_rel).name
                    self.source.fetch(cover_rel, local)
                    if local.stat().st_size <= 16 * 1024 * 1024:
                        info = probe(str(local))
                        w = h = 0
                        if info:
                            for s in info.get("streams", ()):
                                if s.get("codec_type") == "video":
                                    w = int(s.get("width") or 0)
                                    h = int(s.get("height") or 0)
                                    break
                        mime = MIME_BY_EXT.get(
                            PurePosixPath(cover_rel).suffix.lower(), "image/jpeg")
                        result = (local.read_bytes(), mime, w, h)
            except Exception:
                result = None
        with self.cover_lock:
            self.cover_cache[d] = result
        return result


# AAC is wider than MP3 — up to 96 kHz, surround intact — but still a fixed set
# of rates and sample formats, and a 176.4 kHz DSD-sourced master is not in it.
# aformat pins what the encoder will take and lets ffmpeg insert the resampler
# itself, so there is no probe-and-retry path here. Surround is kept: unlike
# libmp3lame, AAC-LC carries 5.1 natively.
AAC_RATES = ("96000|88200|64000|48000|44100|32000|24000|22050"
             "|16000|12000|11025|8000|7350")
AAC_LAYOUTS = "mono|stereo|3.0|4.0|quad|5.0|5.1|6.1|7.1"
AAC_FILTER = (f"aformat=sample_fmts=fltp|s16"
              f":sample_rates={AAC_RATES}"
              f":channel_layouts={AAC_LAYOUTS}")

# libfdk_aac is the better encoder by a clear margin, but it is not in a stock
# ffmpeg build — its licence keeps it out of the distro package. `auto` takes
# it when it is there and falls back to ffmpeg's own encoder when it is not.
AAC_ENCODERS = ("libfdk_aac", "aac")

# libfdk's VBR scale is 1 (worst) to 5 (best). ffmpeg's native encoder has no
# such thing — it takes a global quality instead — so `--vbr N` is mapped onto
# it, keeping one flag that means the same thing whichever encoder is in use.
NATIVE_VBR_Q = {1: 0.5, 2: 0.9, 3: 1.2, 4: 1.6, 5: 2.0}

_encoder_lock = threading.Lock()
_encoder_cache: dict[str, str] = {}


def resolve_encoder(want: str = "auto") -> str:
    """Pick the AAC encoder to use, checking once what this ffmpeg has."""
    with _encoder_lock:
        if want in _encoder_cache:
            return _encoder_cache[want]
        r = run(["ffmpeg", "-hide_banner", "-encoders"], timeout=30)
        have = r.stdout.decode(errors="replace")
        if want == "auto":
            picked = next((e for e in AAC_ENCODERS if f" {e} " in have), "aac")
        elif f" {want} " not in have:
            raise Fatal(f"this ffmpeg has no {want} encoder "
                        f"(available: {', '.join(e for e in AAC_ENCODERS if f' {e} ' in have)})")
        else:
            picked = want
        _encoder_cache[want] = picked
        return picked


def encode_cmd(url: str, out: Path, bitrate: str, vbr: int | None = None,
               encoder: str = "aac") -> list[str]:
    cmd = ["ffmpeg", "-v", "error", "-y", "-i", url, "-map", "0:a:0", "-vn",
           "-af", AAC_FILTER, "-c:a", encoder]
    if vbr is not None:
        if encoder == "libfdk_aac":
            cmd += ["-vbr", str(vbr)]
        else:
            cmd += ["-q:a", str(NATIVE_VBR_Q[vbr])]
    else:
        cmd += ["-b:a", bitrate]
    # -map_metadata 0 carries what the MP4 muxer has an atom for; the rest is
    # written afterwards by write_freeform_tags.
    # +faststart moves the moov atom to the front, which every streaming player
    # and a good few car head units need in order to start without buffering
    # the whole file.
    cmd += ["-map_metadata", "0", "-movflags", "+faststart", "-f", "ipod",
            str(out)]
    return cmd


# Each ffmpeg sftp:// read opens its own SSH connection — it cannot share the
# ControlMaster — so a wide run trips OpenSSH's MaxStartups and the server
# resets connections. These are transient and worth retrying.
TRANSIENT_MARKERS = (
    "Connection reset by peer",
    "Socket error",
    "Connection failed",
    "Input/output error",
    "Connection timed out",
    "Broken pipe",
)


def is_transient(stderr: bytes | str) -> bool:
    text = stderr.decode(errors="replace") if isinstance(stderr, bytes) else stderr
    return any(m in text for m in TRANSIENT_MARKERS)


def run_with_retry(cmd: list[str], attempts: int = 4) -> subprocess.CompletedProcess:
    """Retry a command while it keeps failing for transient network reasons."""
    delay = 1.0
    for attempt in range(attempts):
        r = run(cmd)
        if r.returncode == 0 or not is_transient(r.stderr) or attempt == attempts - 1:
            return r
        time.sleep(delay + random.random())
        delay *= 2
    return r


def do_transcode(ctx: Context, job: Job, tmpdir: Path) -> int:
    url = ctx.source.ffmpeg_url(job.rel)
    out = tmpdir / "out.m4a"
    r = run_with_retry(encode_cmd(url, out, ctx.bitrate, ctx.vbr, ctx.encoder))

    if r.returncode != 0 or not out.exists() or out.stat().st_size == 0:
        msg = r.stderr.decode(errors="replace").strip()[:400]
        raise RuntimeError(f"ffmpeg: {msg or 'produced no output'}")

    # One probe serves both the leftover tags and the embedded picture.
    info = probe(url) if (ctx.tags or ctx.art) else None

    if ctx.tags and info:
        try:
            write_freeform_tags(out, info.get("format", {}).get("tags", {}))
        except Exception as e:  # metadata is nice to have, never fatal
            print(f"\n  extra tags skipped ({e}): {job.rel}", file=sys.stderr)

    if ctx.art:
        cover = (extract_embedded_cover(url, tmpdir, info)
                 or ctx.folder_cover(job.rel))
        if cover:
            try:
                embed_cover(out, cover)
            except Exception as e:  # art is nice to have, never fatal
                print(f"\n  cover art skipped ({e}): {job.rel}", file=sys.stderr)

    size = out.stat().st_size
    ctx.dest.store(out, job.dst_rel, job.mtime)
    return size


def do_copy(ctx: Context, job: Job, tmpdir: Path) -> int:
    if isinstance(ctx.source, LocalEndpoint) and isinstance(ctx.dest, LocalEndpoint):
        src = ctx.source.path(job.rel)
        dst = ctx.dest.path(job.dst_rel)
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(dst.name + TMP_SUFFIX)
        try:
            shutil.copyfile(src, tmp)
            os.utime(tmp, (job.mtime, job.mtime))
            os.replace(tmp, dst)
        finally:
            tmp.unlink(missing_ok=True)
        return dst.stat().st_size

    local = tmpdir / PurePosixPath(job.rel).name
    ctx.source.fetch(job.rel, local)
    size = local.stat().st_size
    ctx.dest.store(local, job.dst_rel, job.mtime)
    return size


# --- progress -----------------------------------------------------------------

class Progress:
    def __init__(self, total: int, label: str = "") -> None:
        self.total = max(total, 1)
        self.label = label
        self.done = self.failed = 0
        self.bytes_in = self.bytes_out = 0
        self.start = time.monotonic()
        self.lock = threading.Lock()
        self.tty = sys.stderr.isatty()

    def tick(self, bytes_in: int = 0, bytes_out: int = 0, failed: bool = False) -> None:
        with self.lock:
            self.done += 1
            self.bytes_in += bytes_in
            self.bytes_out += bytes_out
            self.failed += bool(failed)
            if self.tty or self.done % 200 == 0 or self.done == self.total:
                self._draw()

    def _draw(self) -> None:
        elapsed = time.monotonic() - self.start
        rate = self.done / elapsed if elapsed else 0
        eta = time.strftime("%H:%M:%S",
                            time.gmtime((self.total - self.done) / rate if rate else 0))
        io = f" {human(self.bytes_in)}->{human(self.bytes_out)}" if self.bytes_out else ""
        fail = f" {self.failed} failed" if self.failed else ""
        line = (f"{self.label}{self.done}/{self.total} "
                f"({100 * self.done / self.total:.1f}%){io} "
                f"{rate:.1f}/s eta {eta}{fail}")
        sys.stderr.write(f"\r{line}   " if self.tty else line + "\n")
        sys.stderr.flush()

    def finish(self) -> None:
        if self.tty:
            sys.stderr.write("\n")
            sys.stderr.flush()


def pmap(fn, items, workers: int) -> None:
    """Run fn over items in a thread pool, stopping cleanly on Ctrl-C."""
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(fn, it) for it in items]
        try:
            for f in futures:
                f.result()
        except KeyboardInterrupt:
            _stop.set()
            for f in futures:
                f.cancel()
            raise


# --- commands -----------------------------------------------------------------

def resolve_jobs(args, *endpoints: Endpoint) -> None:
    """Pick a worker count, unless the user or config already chose one."""
    if args.jobs is not None:
        return
    args.jobs = min(os.cpu_count() or 4, 24)
    if any(e.is_remote for e in endpoints):
        args.jobs = min(args.jobs, REMOTE_JOB_CAP)
        print(f"remote endpoint: limiting to {args.jobs} workers "
              f"(override with -j)", file=sys.stderr)


def load_sides(args) -> tuple[Endpoint, Endpoint, Listing, Listing]:
    source = parse_endpoint(args.source, args.port)
    dest = parse_endpoint(args.dest, args.port)
    resolve_jobs(args, source, dest)
    if not source.exists():
        raise Fatal(f"source does not exist: {source.describe()}")
    if not dest.exists():
        if getattr(args, "command", "") == "sync":
            dest.ensure()
        else:
            raise Fatal(f"destination does not exist: {dest.describe()}")
    print(f"scanning {source.describe()} ...", file=sys.stderr)
    src_listing = source.listing()
    print(f"scanning {dest.describe()} ...", file=sys.stderr)
    dst_listing = dest.listing()
    return source, dest, src_listing, dst_listing


def cmd_sync(args) -> int:
    source, dest, src_listing, dst_listing = load_sides(args)
    adopted: list[Job] | None = [] if args.adopt and not args.force else None
    jobs, pending, ignored, dropped = plan(
        src_listing, source, dst_listing, args.force, args.max_duration, args.jobs,
        args.keep_long, adopted)

    if adopted:
        print(f"{len(adopted)} destination files"
              f"{' would be' if args.dry_run else ''} restamped, not re-encoded",
              file=sys.stderr)
        if not args.dry_run:
            failed = dest.restamp([(j.dst_rel, j.mtime) for j in adopted])
            if failed:
                print(f"  {len(failed)} could not be restamped", file=sys.stderr)

    if dropped:
        print(f"{len(dropped)} files skipped, over the "
              f"{format_duration(args.max_duration)} limit:", file=sys.stderr)
        for rel, secs in dropped[:10]:
            print(f"  {format_duration(secs)}  {rel}", file=sys.stderr)
        if len(dropped) > 10:
            print(f"  ... and {len(dropped) - 10} more", file=sys.stderr)

    n_trans = sum(1 for j in pending if j.action == "transcode")
    todo_bytes = sum(j.size for j in pending)
    print(
        f"{len(jobs)} files tracked, {ignored} ignored, "
        f"{len(jobs) - len(pending)} already current\n"
        f"{len(pending)} to process ({human(todo_bytes)}): "
        f"{n_trans} transcode, {len(pending) - n_trans} copy",
        file=sys.stderr,
    )

    if args.delete:
        expected = {fold(j.dst_rel) for j in jobs}
        orphans = [r for r in dst_listing
                   if fold(r) not in expected and r != FAILURE_LOG]
        for rel in orphans[:40]:
            print(f"  orphan: {rel}", file=sys.stderr)
        if len(orphans) > 40:
            print(f"  ... and {len(orphans) - 40} more", file=sys.stderr)
        if orphans and not args.dry_run:
            dest.unlink_many(orphans)
            dest.prune_empty_dirs()
        print(f"{len(orphans)} orphaned destination files"
              f"{' would be' if args.dry_run else ''} removed", file=sys.stderr)

    if not pending:
        return 0
    if args.dry_run:
        for j in pending[:40]:
            print(f"  {j.action:9} {j.rel}")
        if len(pending) > 40:
            print(f"  ... and {len(pending) - 40} more")
        return 0

    ctx = Context(source, dest, args.bitrate, art=not args.no_art, vbr=args.vbr,
                  encoder=resolve_encoder(args.encoder), tags=not args.no_tags,
                  covers=cover_candidates(src_listing) if not args.no_art else {})
    progress = Progress(len(pending))
    failures: list[tuple[str, str]] = []
    lock = threading.Lock()

    def work(job: Job) -> None:
        if _stop.is_set():
            return
        try:
            parent = dest.path(job.dst_rel).parent if isinstance(dest, LocalEndpoint) else None
            if parent is not None:
                parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(dir=parent, prefix=".r2a-") as td:
                tmpdir = Path(td)
                out_size = (do_transcode(ctx, job, tmpdir) if job.action == "transcode"
                            else do_copy(ctx, job, tmpdir))
            progress.tick(job.size, out_size)
        except Exception as e:
            with lock:
                failures.append((job.rel, str(e)))
            progress.tick(failed=True)

    pmap(work, pending, args.jobs)
    progress.finish()

    if failures:
        log = Path.cwd() / FAILURE_LOG
        with log.open("w") as fh:
            for rel, err in failures:
                fh.write(f"{rel}\n    {err}\n")
        print(f"{len(failures)} failures — details in {log}", file=sys.stderr)

    if progress.bytes_in:
        print(f"done: {human(progress.bytes_in)} -> {human(progress.bytes_out)} "
              f"({100 * progress.bytes_out / progress.bytes_in:.1f}% of source)",
              file=sys.stderr)
    return 1 if failures else 0


# ffmpeg logs these at error level, but they say nothing about audio integrity —
# duplicate timestamps are common in gapless and live rips and play back fine.
BENIGN_DECODE_NOISE = (
    "non monotonically increasing dts",
    "timestamps are unset",
    "queue input is backward in time",
)


def decode_errors(stderr: str) -> str:
    """Drop known-harmless ffmpeg chatter, keep anything that signals damage."""
    real = [
        line for line in stderr.splitlines()
        if line.strip() and not any(n in line for n in BENIGN_DECODE_NOISE)
    ]
    return "\n".join(real).strip()


def check_one(ctx: Context, job: Job, tolerance: float) -> str | None:
    """Return an error string if the destination is not a sound mirror."""
    dst_url = ctx.dest.ffmpeg_url(job.dst_rel)

    if job.action == "copy":
        return None  # byte-for-byte copies are covered by the size check below

    info = probe(dst_url)
    if info is None:
        return "destination unreadable by ffprobe"
    st = audio_stream(info)
    if st is None or st.get("codec_name") != "aac":
        return f"not aac ({st.get('codec_name') if st else 'no audio stream'})"

    # Full decode of the audio alone — catches truncation and bit rot that a
    # header probe misses. Cover art streams are skipped: their timestamps are
    # not meaningful and decoding them only produces noise.
    r = run(["ffmpeg", "-v", "error", "-i", dst_url, "-map", "0:a:0", "-f", "null", "-"])
    err = decode_errors(r.stderr.decode(errors="replace"))
    if r.returncode != 0 or err:
        return f"decode error: {err[:200] or 'non-zero exit'}"

    src_info = probe(ctx.source.ffmpeg_url(job.rel))
    if src_info:
        sd, dd = duration_of(src_info), duration_of(info)
        if sd and dd and abs(sd - dd) > tolerance:
            return f"duration mismatch: source {sd:.2f}s vs aac {dd:.2f}s"
    return None


def verify(ctx: Context, jobs: list[Job], dst_listing: dict[str, Entry],
           workers: int, tolerance: float) -> list[tuple[Job, str]]:
    bad: list[tuple[Job, str]] = []
    lock = threading.Lock()

    # Cheap structural checks first, so the expensive decode only runs on files
    # that are actually present and plausible.
    decode_me: list[Job] = []
    for job in jobs:
        entry = dst_listing.get(job.dst_rel)
        if entry is None:
            bad.append((job, "destination missing"))
        elif entry.size == 0:
            bad.append((job, "destination is empty"))
        elif job.action == "copy" and entry.size != job.size:
            bad.append((job, f"copy size differs: {job.size} vs {entry.size}"))
        else:
            decode_me.append(job)

    progress = Progress(len(decode_me), label="verify ")

    def work(job: Job) -> None:
        if _stop.is_set():
            return
        err = check_one(ctx, job, tolerance)
        if err:
            with lock:
                bad.append((job, err))
        progress.tick(failed=bool(err))

    pmap(work, decode_me, workers)
    progress.finish()
    return bad


def cmd_verify(args) -> int:
    source, dest, src_listing, dst_listing = load_sides(args)
    jobs, _, _, _ = plan(src_listing, source, dst_listing, False,
                         args.max_duration, args.jobs, args.keep_long)
    ctx = Context(source, dest, "256k", art=False)
    print(f"verifying {len(jobs)} files (full decode) ...", file=sys.stderr)
    bad = verify(ctx, jobs, dst_listing, args.jobs, args.tolerance)
    if not bad:
        print(f"all {len(jobs)} files verified clean", file=sys.stderr)
        return 0
    print(f"{len(bad)} problems:", file=sys.stderr)
    for job, err in bad:
        print(f"  {job.rel}\n    {err}", file=sys.stderr)
    return 1


def cmd_reclaim(args) -> int:
    source, dest, src_listing, dst_listing = load_sides(args)
    jobs, _, _, _ = plan(src_listing, source, dst_listing, False,
                         args.max_duration, args.jobs, args.keep_long)

    # Only ever delete sources that were transcoded. Copied-through files and
    # sidecars are byte-identical in the destination, so deleting the source
    # would not save meaningful space while removing a real copy.
    targets = [j for j in jobs if j.action == "transcode"]
    ctx = Context(source, dest, "256k", art=False)

    print(f"{len(targets)} transcoded sources are deletion candidates "
          f"({human(sum(j.size for j in targets))})", file=sys.stderr)
    print("verifying every one before deleting anything ...", file=sys.stderr)

    bad = verify(ctx, targets, dst_listing, args.jobs, args.tolerance)
    bad_rels = {j.rel for j, _ in bad}
    safe = [j for j in targets if j.rel not in bad_rels]
    freed = sum(j.size for j in safe)

    if bad:
        print(f"\n{len(bad)} files FAILED verification — their sources stay put:",
              file=sys.stderr)
        for job, err in bad[:20]:
            print(f"  {job.rel}\n    {err}", file=sys.stderr)
        if len(bad) > 20:
            print(f"  ... and {len(bad) - 20} more", file=sys.stderr)

    print(f"\n{len(safe)} sources verified safe to delete, freeing {human(freed)}",
          file=sys.stderr)
    if not safe:
        return 1 if bad else 0

    if not args.yes:
        print("DRY RUN — nothing deleted. Re-run with --yes to delete for real.",
              file=sys.stderr)
        return 0

    print(f"deleting {len(safe)} files from {source.describe()} ...", file=sys.stderr)
    failed = source.unlink_many([j.rel for j in safe])
    print(f"deleted {len(safe) - len(failed)} source files, freed {human(freed)}"
          + (f", {len(failed)} could not be removed" if failed else ""),
          file=sys.stderr)

    if args.prune_empty:
        print(f"removed {source.prune_empty_dirs()} empty source directories",
              file=sys.stderr)
    return 1 if bad else 0


def cmd_prune(args) -> int:
    """Apply a duration limit to an already-synced tree, after the fact.

    Deliberately works on the destination alone: the whole point is to clean up
    a library that was converted before a limit existed, and by then the source
    may well be gone.
    """
    target_spec = args.target or getattr(args, "dest", None)
    if not target_spec:
        raise Fatal("no target given (pass a path or set `dest` in the config)")
    if args.max_duration <= 0:
        raise Fatal("no duration limit set (use --max-duration 25m "
                    "or set `max_duration` in the config)")

    target = parse_endpoint(target_spec, args.port)
    resolve_jobs(args, target)
    if not target.exists():
        raise Fatal(f"target does not exist: {target.describe()}")

    print(f"scanning {target.describe()} ...", file=sys.stderr)
    listing = target.listing()
    exempt = exempt_matcher(args.keep_long)
    audio = {rel: e for rel, e in listing.items()
             if PurePosixPath(rel).suffix.lower() in AUDIO_EXT and not exempt(rel)}
    spared = sum(1 for rel in listing
                 if PurePosixPath(rel).suffix.lower() in AUDIO_EXT and exempt(rel))
    print(f"{len(listing)} files, {len(audio)} audio"
          + (f", {spared} exempt via keep_long" if spared else ""),
          file=sys.stderr)

    limit = args.max_duration
    print(f"probing durations against a {format_duration(limit)} limit ...",
          file=sys.stderr)
    progress = Progress(len(audio), label="probe ")
    over: list[tuple[str, float, int]] = []
    unreadable = 0
    lock = threading.Lock()

    def work(item: tuple[str, Entry]) -> None:
        nonlocal unreadable
        if _stop.is_set():
            return
        rel, entry = item
        d = probe_duration(target.ffmpeg_url(rel))
        with lock:
            if d is None:
                unreadable += 1
            elif d > limit:
                over.append((rel, d, entry.size))
        progress.tick()

    pmap(work, sorted(audio.items()), args.jobs)
    progress.finish()

    if unreadable:
        print(f"{unreadable} files had no readable duration and were left alone",
              file=sys.stderr)
    if not over:
        print(f"nothing exceeds {format_duration(limit)} — nothing to do",
              file=sys.stderr)
        return 0

    over.sort(key=lambda t: -t[1])
    freed = sum(size for _, _, size in over)
    print(f"\n{len(over)} files exceed {format_duration(limit)} "
          f"({human(freed)}):", file=sys.stderr)
    for rel, secs, size in over:
        print(f"  {format_duration(secs):>8}  {human(size):>7}  {rel}",
              file=sys.stderr)

    if not args.yes:
        print(f"\nDRY RUN — nothing deleted. Re-run with --yes to delete "
              f"these {len(over)} files.", file=sys.stderr)
        return 0

    failed = target.unlink_many([rel for rel, _, _ in over])
    deleted = len(over) - len(failed)
    print(f"\ndeleted {deleted} files, freed {human(freed)}"
          + (f", {len(failed)} could not be removed" if failed else ""),
          file=sys.stderr)

    # A pruned mix often leaves its cover art behind with no audio beside it.
    gone = {rel for rel, _, _ in over}
    remaining = {str(PurePosixPath(r).parent) for r in listing if r not in gone}
    stripped = {str(PurePosixPath(rel).parent) for rel in gone}
    orphaned = [d for d in stripped if d in remaining and not any(
        PurePosixPath(r).suffix.lower() in AUDIO_EXT
        for r in listing if r not in gone and str(PurePosixPath(r).parent) == d)]
    if orphaned:
        print(f"{len(orphaned)} directories now hold sidecars but no audio "
              f"(cover art, cue sheets) — left in place:", file=sys.stderr)
        for d in sorted(orphaned)[:10]:
            print(f"  {d}", file=sys.stderr)
        if len(orphaned) > 10:
            print(f"  ... and {len(orphaned) - 10} more", file=sys.stderr)

    if args.prune_empty:
        print(f"removed {target.prune_empty_dirs()} empty directories",
              file=sys.stderr)
    return 0


# --- cli ----------------------------------------------------------------------

def build_parser(config: dict | None = None) -> argparse.ArgumentParser:
    config = config or {}
    subparsers: list[argparse.ArgumentParser] = []
    p = argparse.ArgumentParser(
        prog="rsync2aac",
        description="Mirror a lossless music library into AAC, incrementally.",
        epilog="Endpoints may be local paths or rsync-style [user@]host:/path.",
    )
    p.add_argument("--version", action="version", version=f"rsync2aac {__version__}")
    p.add_argument("--config", "-c", metavar="PATH",
                   help=f"config file (default: ./{CONFIG_NAME}, "
                        f"then ~/.config/{CONFIG_DIR}/{CONFIG_NAME})")
    p.add_argument("--jobs", "-j", type=int, default=None,
                   help=f"parallel workers (default: {min(os.cpu_count() or 4, 24)} "
                        f"local, {REMOTE_JOB_CAP} when an endpoint is remote)")
    p.add_argument("--port", "-P", type=int, default=None, help="ssh port")
    sub = p.add_subparsers(dest="command", required=True)

    def endpoints(sp):
        subparsers.append(sp)
        sp.add_argument("source", nargs="?",
                        help="source tree (local path or host:/path)")
        sp.add_argument("dest", nargs="?",
                        help="destination tree (local path or host:/path)")
        sp.add_argument("--max-duration", "-t", metavar="DUR",
                        help="skip audio longer than this entirely, "
                             "e.g. 25m, 1h30m, 90s (default: no limit)")
        sp.add_argument("--keep-long", metavar="PATTERN", action="append",
                        default=[],
                        help="exempt a path or glob from --max-duration "
                             "(repeatable)")

    sp = sub.add_parser("sync", help="convert and copy everything missing or stale")
    endpoints(sp)
    sp.add_argument("--bitrate", "-b", default="192k",
                    help="constant target bitrate (default: %(default)s)")
    sp.add_argument("--vbr", "-V", type=int, metavar="1-5", default=None,
                    help="use VBR at this quality instead of --bitrate "
                         "(1 worst, 5 best; 4 is roughly 192k)")
    sp.add_argument("--encoder", "-e", default="auto",
                    choices=("auto", *AAC_ENCODERS),
                    help="aac encoder to use (default: %(default)s — libfdk_aac "
                         "if this ffmpeg has it, otherwise the native one)")
    sp.add_argument("--no-tags", action="store_true",
                    help="skip the freeform-atom pass that carries ReplayGain, "
                         "label, ISRC and other tags MP4 has no atom for")
    sp.add_argument("--dry-run", "-n", action="store_true",
                    help="show what would happen, change nothing")
    sp.add_argument("--force", "-f", action="store_true",
                    help="re-encode even files that look current")
    sp.add_argument("--delete", action="store_true",
                    help="remove destination files whose source is gone")
    sp.add_argument("--no-art", action="store_true", help="skip cover art embedding")
    sp.add_argument("--adopt", action="store_true",
                    help="trust existing destination files: restamp them with the "
                         "source mtime instead of re-encoding (use after the source "
                         "tree was copied without preserving timestamps)")
    sp.set_defaults(func=cmd_sync)

    sp = sub.add_parser("verify", help="full-decode every destination file")
    endpoints(sp)
    sp.add_argument("--tolerance", type=float, default=1.0,
                    help="allowed duration drift in seconds (default: %(default)s)")
    sp.set_defaults(func=cmd_verify)

    sp = sub.add_parser("reclaim",
                        help="verify, then delete source files that are safely mirrored")
    endpoints(sp)
    sp.add_argument("--tolerance", type=float, default=1.0)
    sp.add_argument("--yes", action="store_true",
                    help="actually delete (without this it is a dry run)")
    # Accepted for symmetry with `sync -n`; these commands are dry by default.
    sp.add_argument("--dry-run", "-n", action="store_true",
                    help="explicit no-op: this command is already a dry run "
                         "unless --yes is given")
    sp.add_argument("--prune-empty", action="store_true",
                    help="also remove source directories left empty")
    sp.set_defaults(func=cmd_reclaim)

    sp = sub.add_parser(
        "prune",
        help="apply a duration limit to an already-synced tree, after the fact")
    subparsers.append(sp)
    sp.add_argument("target", nargs="?",
                    help="tree to prune (defaults to `dest` from the config)")
    sp.add_argument("--max-duration", "-t", metavar="DUR",
                    help="delete audio longer than this, e.g. 25m")
    sp.add_argument("--keep-long", metavar="PATTERN", action="append", default=[],
                    help="exempt a path or glob from the limit (repeatable)")
    sp.add_argument("--yes", action="store_true",
                    help="actually delete (without this it is a dry run)")
    # Accepted for symmetry with `sync -n`; these commands are dry by default.
    sp.add_argument("--dry-run", "-n", action="store_true",
                    help="explicit no-op: this command is already a dry run "
                         "unless --yes is given")
    sp.add_argument("--prune-empty", action="store_true",
                    help="also remove directories left empty")
    sp.set_defaults(func=cmd_prune, needs_endpoints=False)

    # Config values become defaults on the subparsers too. Setting them only on
    # the top-level parser is not enough: a subparser's own defaults are applied
    # afterwards and would clobber them.
    if config:
        p.set_defaults(**config)
        for sp in subparsers:
            sp.set_defaults(**config)
    return p


def main(argv: list[str] | None = None) -> int:
    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            print(f"rsync2aac: {tool} not found in PATH", file=sys.stderr)
            return 2

    try:
        # --config has to be resolved before the main parse, so that config
        # values can become argparse defaults and CLI flags still win.
        pre = argparse.ArgumentParser(add_help=False)
        pre.add_argument("--config", "-c")
        known, _ = pre.parse_known_args(argv)
        config, config_path = load_config(known.config)

        parser = build_parser(config)
        args = parser.parse_args(argv)

        if config_path:
            print(f"using config {config_path}", file=sys.stderr)
        if getattr(args, "needs_endpoints", True) and (
                args.source is None or args.dest is None):
            parser.error(
                "source and dest are required (pass them as arguments or set "
                "them in a config file)")
        args.max_duration = parse_duration(getattr(args, "max_duration", 0) or 0)
        vbr = getattr(args, "vbr", None)
        if vbr is not None and vbr not in NATIVE_VBR_Q:
            parser.error("--vbr takes a quality from 1 (worst) to 5 (best)")
    except Fatal as e:
        print(f"rsync2aac: {e}", file=sys.stderr)
        return 2

    signal.signal(signal.SIGINT, lambda *_: (_stop.set(), sys.exit(130)))
    try:
        return args.func(args)
    except Fatal as e:
        print(f"rsync2aac: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())

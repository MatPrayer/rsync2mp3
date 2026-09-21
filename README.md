# rsync2opus

Mirror a lossless music library into Opus, incrementally, the way rsync mirrors files.

Point it at a source and a destination and run it as often as you like — only
what changed gets processed. Either side may be local or remote.

```sh
rsync2opus sync /music /music-opus
rsync2opus sync nas:/srv/music /music-opus
rsync2opus sync /music user@nas:/srv/music-opus
```

## Why Opus

Measured on a 2:44 FLAC track (874 kbps, 18 MB) with 26 metadata tags and
embedded cover art:

| Codec | Settings | Size | Tags kept | Cover art |
|---|---|---|---|---|
| **Opus** | `libopus` 128k VBR | **2.7 MB** | **26/26, unchanged** | yes, via mutagen |
| MP3 | `libmp3lame` V0 | 6.3 MB | 25/26, remapped to TXXX | yes, native |
| AAC | `aac` 256k | 6.4 MB | 11/26 — ReplayGain, label, disc/track totals and release date all lost | yes, native |

Opus is half the size at better perceptual quality, and it is the only one of
the three with lossless tag fidelity: Opus stores Vorbis comments, exactly the
same tag model FLAC uses, so nothing is remapped or dropped on the way across.
(The AAC row is what ffmpeg does on its own; `rsync2aac` puts the missing tags
back by hand.)

The tradeoff is playback support. Opus is fine on Android, Linux, VLC,
foobar2000, Poweramp, Symfonium, Jellyfin and Rockbox. It will not play on most
car head units, older DAPs, Sonos, or the stock iOS Music app.

For those there are two siblings, the same tool with a different encoder on the
output side:

- **[rsync2mp3](README-rsync2mp3.md)** — plays on anything with a USB port.
  Twice the bitrate for the same quality.
- **[rsync2aac](README-rsync2aac.md)** — the Apple target: iPhones, iPods,
  CarPlay, older Sonos. Keeps surround, and keeps the tags MP4 has no atom for.

## Install

```sh
./install.sh          # symlinks into ~/.local/bin
```

or, for an isolated install:

```sh
pipx install .
```

Needs `ffmpeg` (with `libopus`), `ffprobe`, and `python-mutagen`:

```sh
sudo pacman -S ffmpeg python-mutagen   # Arch
brew install ffmpeg                    # macOS
python3 -m pip install --user mutagen  # macOS: mutagen is not in Homebrew
```

Linux and macOS are both supported, including a macOS box on either end of a
remote sync. Python 3.11 or newer (for `tomllib`).

## How files are treated

| Source | Result |
|---|---|
| `.flac .wav .aiff .ape .wv .tta .tak .shn .dsf .dff .w64 .caf` | transcoded to Opus |
| `.m4a .m4b .mp4` | probed — ALAC/PCM is transcoded, AAC is copied |
| `.mp3 .opus .ogg .aac .mpc .wma` | copied verbatim, never re-encoded |
| `.jpg .png .webp .lrc .cue .m3u .m3u8 .pls .pdf` | copied |
| `.log .nfo .sfv .accurip .md5`, dotfiles | ignored |

Cover art is embedded in each `.opus` file: the source's own attached picture
if it has one, otherwise the best `cover`/`folder`/`front` image sitting in the
album directory.

## Configuration

Settings live in a TOML file, looked up in this order — first hit wins:

1. `--config PATH`
2. `./rsync2opus.toml`
3. `$XDG_CONFIG_HOME/rsync2opus/config.toml`, if that variable is set
4. `~/Library/Application Support/rsync2opus/config.toml` (macOS only)
5. `~/.config/rsync2opus/config.toml`

Command-line flags always override the config file.

```toml
source = "/path/to/music"
dest   = "/path/to/music-opus"

bitrate      = "128k"
# vbr        = "on"     # on | constrained | off
# compression_level = 10
max_duration = "25m"    # skip anything longer, entirely
art          = true
# jobs       = 24
# delete     = false
# tolerance  = 1.0
# port       = 22
```

With `source` and `dest` set, `rsync2opus sync` runs with no arguments.
Unknown keys are rejected with the list of valid ones rather than silently
ignored.

### Skipping long files

`max_duration` (or `-t/--max-duration`) drops audio longer than the limit
**completely**: not converted, not copied, and never deleted by `reclaim`.
Accepts `25m`, `1h30m`, `90s`, or bare seconds.

```
probing durations of 8654 audio files (max_duration is set) ...
24 files skipped, over the 25:00 limit:
  2:13:03  DJ Mixes/Boiler Room/2018 - Tangerine Dream.m4a
  1:03:32  Sleep/1/01 - Dopesmoker.opus
```

Working out a duration means probing every audio file, so this only runs when
a limit is set — leave it unset and it costs nothing. A file whose duration
can't be read is kept rather than skipped, so an unreadable header never
silently drops music.

Because skipped files are excluded from the plan entirely, `reclaim` will never
delete their sources, and `sync --delete` will prune them from the destination
if they were converted under an earlier, looser limit.

### Exempting long-form music

A duration threshold cannot tell an hour-long DJ set from an hour-long doom
metal album. `keep_long` exempts specific paths from the limit:

```toml
keep_long = [
  "Sleep/1/01 - Dopesmoker.opus",
  "TURQUOISEDEATH/*/*Close Your Eyes.opus",
]
```

Each entry is matched first as a literal relative path, then as a glob — the
literal check comes first because real filenames are full of brackets, which
glob syntax would otherwise read as character classes. Also available as
`--keep-long PATTERN`, repeatable.

## Commands

### `sync`

```sh
rsync2opus sync SOURCE DEST [-b 128k] [-t 25m] [-n] [-f] [--delete] [--no-art]
```

- `-n, --dry-run` — show what would happen, change nothing
- `-b, --bitrate` — Opus target bitrate, default `128k`
- `--vbr` — `on` (default), `constrained`, or `off` for a true CBR stream
- `--compression-level` — encoder effort, 0 fastest to 10 best (default `10`);
  this buys quality with encoding time, not with file size
- `-t, --max-duration` — skip audio longer than this, default no limit
- `-f, --force` — re-encode even files that look current
- `--delete` — remove destination files whose source is gone
- `--no-art` — skip cover art embedding (faster)

Freshness is decided by mtime, the way rsync decides it: each destination file
carries its source's timestamp, so a re-run only picks up new and changed
files. There is no state database to lose or corrupt. Every write goes to a
temp file and is renamed into place, so an interrupted run never leaves a
half-written track behind — just run it again.

### `verify`

```sh
rsync2opus verify SOURCE DEST [--tolerance 1.0]
```

Fully decodes every destination file and compares its duration against the
source. Catches truncation and corruption that a header probe would miss.

### `reclaim`

```sh
rsync2opus reclaim SOURCE DEST [--yes] [--prune-empty]
```

Verifies everything, then deletes the source files that are provably mirrored.
**Dry run unless you pass `--yes`.** Only transcoded sources are ever deleted —
files that were copied verbatim are left alone, since deleting them would
remove a real copy without saving meaningful space. Anything that fails
verification keeps its source.

### `prune`

```sh
rsync2opus prune [TARGET] [-t 25m] [--yes] [--prune-empty]
```

Applies a duration limit to an **already-synced** tree, after the fact — for
when you converted a library before setting a limit. Works on the destination
alone and never looks at the source, which may well be gone by then. Target
defaults to `dest` from the config.

**Dry run unless you pass `--yes`.** It lists every file over the limit with
its duration and size first:

```
10103 files, 8646 audio, 8 exempt via keep_long
16 files exceed 25:00 (1.2G):
   2:13:03   153.8M  DJ Mixes/Boiler Room/2018 - Tangerine Dream.m4a
   1:31:57    83.1M  Skrillex/Full Sets/SKRILLEX B2B ISOXO.opus
```

Files whose duration can't be read are left alone. `keep_long` applies here
too. Deleting a long mix can leave its cover art and cue sheet behind with no
audio beside them, so those directories are reported; `--prune-empty` removes
any left completely empty.

## Remote endpoints

Remote paths use rsync syntax, `[user@]host:/path`, on either side. SSH
connection multiplexing is set up automatically, so a run over thousands of
files pays for one handshake rather than thousands. Reads go through ffmpeg's
`sftp://` protocol so files are decoded in place instead of being downloaded
whole. The remote host needs `find` and an SSH key — `BatchMode` is on, so
there are no password prompts. Both GNU and BSD userlands work: on a macOS
or BSD remote the listing goes through `stat -f` instead of GNU `find
-printf`, and timestamps are set with `touch -t` in UTC instead of `-d
@epoch`. One consequence on those hosts: a filename containing a newline is
skipped rather than risking a mangled path, since BSD `stat` cannot emit
NUL-separated records.

Use `-P, --port` for a non-standard SSH port.

## macOS notes

Everything works the same, with three differences worth knowing about:

- **Accented filenames.** macOS stores names decomposed (NFD) while Linux
  usually stores them composed (NFC), so `Björk` on one side is a different
  byte string from `Björk` on the other. Comparisons are normalised, so a
  library synced between the two does not re-transcode every accented track or
  report it as an orphan. Stored names are left exactly as the filesystem has
  them, and when a destination file already exists under the other spelling,
  its own spelling is reused rather than a second copy created beside it.
- **Config location.** `~/Library/Application Support/<tool>/config.toml` is
  searched, with `~/.config/<tool>/config.toml` still working after it, so a
  config shared with a Linux machine keeps working.
- **SSH control sockets.** A Unix socket path cannot exceed ~104 bytes and
  macOS puts `TMPDIR` under a long `/var/folders/...` path, so the multiplexing
  socket is created under `/tmp` with a fixed-length `%C` hash for a name.

`~/.local/bin` is not on `PATH` by default on macOS; `install.sh` says so and
prints the line to add.

## Options

- `-c, --config PATH` — config file location
- `-j, --jobs N` — parallel workers, defaults to your core count capped at 24
- `-P, --port N` — SSH port
- `--version`

Ctrl-C stops cleanly; completed files stay, partial ones are discarded.
Failures are collected in `.rsync2opus-failures.log` and never abort the run.

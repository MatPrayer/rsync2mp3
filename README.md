# rsync2mp3

Mirror a lossless music library into a lossy one, incrementally, the way rsync
mirrors files.

Point it at a source and a destination and run it as often as you like — only
what changed gets processed. Either side may be local or remote.

```sh
rsync2mp3 sync /music /music-mp3
rsync2mp3 sync nas:/srv/music /music-mp3
rsync2mp3 sync /music user@nas:/srv/music-mp3
```

Three tools, one codebase: the same commands, the same shared config file and
the same mtime-based freshness, with a different encoder on the output side.

| Tool | Output | For | Docs |
|---|---|---|---|
| **rsync2mp3** | MP3 (`libmp3lame`) | the compatibility target — car head units, older DAPs, Sonos, the stock iOS Music app, anything with a USB port on the front | [README-rsync2mp3.md](README-rsync2mp3.md) |
| **rsync2opus** | Opus (`libopus`) | half the size at better quality, lossless tag fidelity — Android, Linux, VLC, foobar2000, Poweramp, Symfonium, Jellyfin, Rockbox | [README-rsync2opus.md](README-rsync2opus.md) |
| **rsync2aac** | AAC in `.m4a` | the Apple target — iPhones, iPods, CarPlay, older Sonos. Keeps surround, and keeps the tags MP4 has no atom for | [README-rsync2aac.md](README-rsync2aac.md) |

Measured on a 2:44 FLAC track (874 kbps, 18 MB) with 26 metadata tags and
embedded cover art:

| Codec | Settings | Size | Tags kept |
|---|---|---|---|
| Opus | `libopus` 128k VBR | 2.7 MB | 26/26, unchanged |
| MP3 | `libmp3lame` V0 | 6.3 MB | 25/26, remapped to TXXX |
| AAC | `aac` 256k | 6.4 MB | 11/26 natively — 26/26 as `rsync2aac` writes it |

Pick by what the player can read, not by the numbers: a file the device refuses
to open is worth nothing at any bitrate.

## Install

```sh
./install.sh          # symlinks all three tools into ~/.local/bin
```

or, for an isolated install:

```sh
pipx install .
```

Needs `ffmpeg`, `ffprobe` and `python-mutagen`:

```sh
sudo pacman -S ffmpeg python-mutagen   # Arch
brew install ffmpeg                    # macOS
python3 -m pip install --user mutagen  # macOS: mutagen is not in Homebrew
```

Linux and macOS are both supported, including a macOS box on either end of a
remote sync. Python 3.11 or newer (for `tomllib`). `install.sh` reports which
encoders your ffmpeg actually has, and warns about the ones it is missing.

## How files are treated

The same classification in all three tools — only the transcode target differs:

| Source | Result |
|---|---|
| `.flac .wav .aiff .ape .wv .tta .tak .shn .dsf .dff .w64 .caf` | transcoded |
| `.m4a .m4b .mp4` | probed — ALAC/PCM is transcoded, AAC is copied |
| `.mp3 .opus .ogg .aac .mpc .wma` | copied verbatim, never re-encoded |
| `.jpg .png .webp .lrc .cue .m3u .m3u8 .pls .pdf` | copied |
| `.log .nfo .sfv .accurip .md5`, dotfiles | ignored |

Already-lossy sources are **copied, not re-encoded** — transcoding lossy to
lossy only throws away quality.

Cover art is embedded in every transcoded file: the source's own attached
picture if it has one, otherwise the best `cover`/`folder`/`front` image sitting
in the album directory. `--no-art` skips it and is faster. The container
details — ID3v2.3 `APIC`, the MP4 `covr` atom, an Opus picture block — are in
each tool's own page.

Surround is kept by `rsync2opus` and `rsync2aac`. MP3 cannot carry it, so
`rsync2mp3` downmixes to stereo and resamples down to 48 kHz; see
[README-rsync2mp3.md](README-rsync2mp3.md#what-mp3-cannot-represent).

### Name collisions

Where the output extension is also a common input one — `.mp3` for `rsync2mp3`,
`.m4a` for `rsync2aac` — an album directory holding both `track.flac` and
`track.mp3` produces two jobs writing the same destination file. The transcode
of the lossless master wins; the lossy twin is skipped and reported:

```
1 sources collide on a destination path and were skipped:
  Album/05.mp3  (kept Album/05.flac)
```

Opus output never collides, since nothing in a lossless library arrives as
`.opus`.

## Configuration

All three tools read **one** TOML file, looked up in this order — first hit
wins:

1. `--config PATH`
2. `./config.toml`
3. `$XDG_CONFIG_HOME/rsync2/config.toml`, if that variable is set
4. `~/Library/Application Support/rsync2/config.toml` (macOS only)
5. `~/.config/rsync2/config.toml`

Top-level keys apply to every tool. The `[mp3]`, `[opus]` and `[aac]` tables
override them, and are where anything codec-specific belongs — each tool reads
only its own table and ignores the others.

```toml
source       = "/path/to/music"
max_duration = "25m"    # skip anything longer, entirely
art          = true
# jobs       = 24
# delete     = false
# tolerance  = 1.0
# port       = 22

[mp3]
dest    = "/path/to/music-mp3"
bitrate = "256k"
# vbr   = 0             # LAME VBR quality; overrides bitrate

[opus]
dest    = "/path/to/music-opus"
bitrate = "128k"

[aac]
dest    = "/path/to/music-aac"
bitrate = "192k"
```

Start from [`config.toml.example`](config.toml.example). With `source` and a
`dest` set, `sync` runs with no arguments. Command-line flags always override
the config file. Unknown keys are rejected with the list of valid ones rather
than silently ignored — except for a top-level key that only a sibling
understands (`encoder`, `tags`), which is ignored, since the file is shared.

### Skipping long files

`max_duration` (or `-t/--max-duration`) drops audio longer than the limit
**completely**: not converted, not copied, and never deleted by `reclaim`.
Accepts `25m`, `1h30m`, `90s`, or bare seconds.

```
probing durations of 8654 audio files (max_duration is set) ...
24 files skipped, over the 25:00 limit:
  2:13:03  DJ Mixes/Boiler Room/2018 - Tangerine Dream.m4a
  1:03:32  Sleep/1/01 - Dopesmoker.mp3
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
[mp3]
keep_long = [
  "Sleep/1/01 - Dopesmoker.mp3",
  "TURQUOISEDEATH/*/*Close Your Eyes.mp3",
]
```

Each entry is matched first as a literal relative path, then as a glob — the
literal check comes first because real filenames are full of brackets, which
glob syntax would otherwise read as character classes. Also available as
`--keep-long PATTERN`, repeatable.

Entries name **destination** paths, which is why the list belongs in a codec
table rather than at the top level: the same track is `.mp3` under `[mp3]`,
`.opus` under `[opus]` and `.m4a` under `[aac]`.

## Commands

Four commands, identical across the three tools. `rsync2mp3` is used in the
examples; substitute `rsync2opus` or `rsync2aac` freely.

### `sync`

```sh
rsync2mp3 sync SOURCE DEST [-b 256k] [-t 25m] [-n] [-f] [--delete] [--no-art] [--adopt]
```

- `-n, --dry-run` — show what would happen, change nothing
- `-b, --bitrate` — target bitrate; the default differs per codec
- `-V, --vbr` — variable bitrate quality; the scale differs per codec
- `--compression-level` — encoder effort
- `-t, --max-duration` — skip audio longer than this, default no limit
- `-f, --force` — re-encode even files that look current
- `--delete` — remove destination files whose source is gone
- `--no-art` — skip cover art embedding (faster)
- `--adopt` — trust the existing destination files and restamp them with the
  source mtime instead of re-encoding; for when the source tree was copied
  without preserving timestamps

Freshness is decided by mtime, the way rsync decides it: each destination file
carries its source's timestamp, so a re-run only picks up new and changed
files. There is no state database to lose or corrupt. Every write goes to a
temp file and is renamed into place, so an interrupted run never leaves a
half-written track behind — just run it again.

### `verify`

```sh
rsync2mp3 verify SOURCE DEST [--tolerance 1.0]
```

Fully decodes every destination file and compares its duration against the
source. Catches truncation and corruption that a header probe would miss.

### `reclaim`

```sh
rsync2mp3 reclaim SOURCE DEST [--yes] [--prune-empty]
```

Verifies everything, then deletes the source files that are provably mirrored.
**Dry run unless you pass `--yes`.** Only transcoded sources are ever deleted —
files that were copied verbatim are left alone, since deleting them would
remove a real copy without saving meaningful space. Anything that fails
verification keeps its source.

### `prune`

```sh
rsync2mp3 prune [TARGET] [-t 25m] [--yes] [--prune-empty]
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
   1:31:57    83.1M  Skrillex/Full Sets/SKRILLEX B2B ISOXO.mp3
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
- **Config location.** `~/Library/Application Support/rsync2/config.toml` is
  searched, with `~/.config/rsync2/config.toml` still working after it, so a
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
Failures are collected in `.rsync2mp3-failures.log` — or the matching
`.rsync2opus-` / `.rsync2aac-` file — and never abort the run.

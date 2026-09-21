# rsync2opus

Mirror a lossless music library into Opus, incrementally, the way rsync mirrors
files. Same tool as [rsync2mp3](README-rsync2mp3.md) — same commands, same
config file, same mtime-based freshness — with `libopus` on the output side.

```sh
rsync2opus sync /music /music-opus
rsync2opus sync nas:/srv/music /music-opus
rsync2opus sync /music user@nas:/srv/music-opus
```

## Why Opus

Measured on the same 2:44 FLAC (874 kbps, 18 MB, 26 tags, embedded art):

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
car head units, older DAPs, Sonos, or the stock iOS Music app — those want
[`rsync2mp3`](README-rsync2mp3.md) or [`rsync2aac`](README-rsync2aac.md).

## Install

```sh
./install.sh          # symlinks all three tools into ~/.local/bin
```

Needs `ffmpeg` built with `libopus`, plus `ffprobe` and `python-mutagen`:

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

Surround **is kept**: the bitrate is scaled up automatically to carry it,
rather than downmixed to stereo the way `rsync2mp3` has to.

Cover art is embedded in each `.opus` file: the source's own attached picture
if it has one, otherwise the best `cover`/`folder`/`front` image sitting in the
album directory. `--no-art` skips it and is faster.

## Bitrate and VBR

```sh
rsync2opus sync src dst              # 128k VBR, the default
rsync2opus sync src dst -b 160k      # higher target
rsync2opus sync src dst --vbr off    # a true CBR stream
```

- `-b, --bitrate` — Opus target bitrate for stereo, default `128k`. With VBR on
  it is a target rather than a ceiling.
- `--vbr` — `on` (default), `constrained`, or `off`.
- `--compression-level` — encoder effort, 0 fastest to 10 best (default `10`);
  this buys quality with encoding time, not with file size.

## Configuration

The shared `config.toml` all three tools read — see
[README.md](README.md#configuration) for the lookup order. Top-level keys apply
everywhere; the `[opus]` table holds what is Opus-specific:

```toml
source       = "user@host:path/to/music"
max_duration = "25m"
art          = true

[opus]
dest    = "/path/to/music-opus"
bitrate = "128k"
# vbr   = "on"          # on | constrained | off
# compression_level = 10
keep_long = [
  "Sleep/1/01 - Dopesmoker.opus",
]
```

Command-line flags always override the config. Unknown keys are rejected with
the list of valid ones. `keep_long` entries name **destination** paths, which
is why each codec table spells the same track with its own extension.

## Commands

Same four as rsync2mp3, with the same semantics:

- **`sync`** — convert and copy everything missing or stale. `-n` dry run,
  `-f` force, `--delete` remove orphans, `--adopt` restamp instead of
  re-encoding, `--no-art`.
- **`verify`** — full-decode every destination file and compare durations
  against the source.
- **`reclaim`** — verify, then delete source files that are provably mirrored.
  Dry run unless `--yes`.
- **`prune`** — apply a duration limit to an already-synced tree, after the
  fact. Dry run unless `--yes`.

See [README.md](README.md) for the full description of each, plus remote
endpoints, `max_duration`, `keep_long`, `--adopt` and the macOS notes.

Failures are collected in `.rsync2opus-failures.log` and never abort the run.

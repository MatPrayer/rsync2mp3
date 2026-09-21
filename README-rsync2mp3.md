# rsync2mp3

Mirror a lossless music library into MP3, incrementally, the way rsync mirrors
files. The default tool of the suite — same commands, same config file, same
mtime-based freshness as its siblings, with `libmp3lame` on the output side.

```sh
rsync2mp3 sync /music /music-mp3
rsync2mp3 sync nas:/srv/music /music-mp3
rsync2mp3 sync /music user@nas:/srv/music-mp3
```

## Why MP3

Because the player says so. MP3 is the compatibility target, not the quality
one: car head units, older DAPs, Sonos, the stock iOS Music app, cheap
bluetooth receivers, anything with a USB port on the front.

Measured on the same 2:44 FLAC (874 kbps, 18 MB, 26 tags, embedded art):

| Codec | Settings | Size | Tags kept |
|---|---|---|---|
| **MP3** | `libmp3lame` V0 | **6.3 MB** | 25/26, remapped to TXXX |
| MP3 | `libmp3lame` 256k CBR | 5.2 MB | 25/26, remapped to TXXX |
| Opus | `libopus` 128k VBR | 2.7 MB | 26/26, unchanged |
| AAC | `aac` 256k | 6.4 MB | 11/26 — ReplayGain, label, disc/track totals and release date all lost |

Tags come across through ffmpeg's ID3 mapping. ID3v2 has no native frame for
things like ReplayGain, so those land in `TXXX` — readable, but a player that
doesn't look there will not find them.

If the device can read something better, there are two siblings:
[`rsync2opus`](README-rsync2opus.md) for half the size at better quality and
lossless tag fidelity, [`rsync2aac`](README-rsync2aac.md) for Apple hardware.

## Install

```sh
./install.sh          # symlinks all three tools into ~/.local/bin
```

Needs `ffmpeg` built with `libmp3lame`, plus `ffprobe` and `python-mutagen`:

```sh
sudo pacman -S ffmpeg python-mutagen   # Arch
brew install ffmpeg                    # macOS
python3 -m pip install --user mutagen  # macOS: mutagen is not in Homebrew
```

Linux and macOS are both supported, including a macOS box on either end of a
remote sync. Python 3.11 or newer (for `tomllib`). `install.sh` warns if your
ffmpeg has no `libmp3lame`.

## How files are treated

| Source | Result |
|---|---|
| `.flac .wav .aiff .ape .wv .tta .tak .shn .dsf .dff .w64 .caf` | transcoded to MP3 |
| `.m4a .m4b .mp4` | probed — ALAC/PCM is transcoded, AAC is copied |
| `.mp3 .opus .ogg .aac .mpc .wma` | copied verbatim, never re-encoded |
| `.jpg .png .webp .lrc .cue .m3u .m3u8 .pls .pdf` | copied |
| `.log .nfo .sfv .accurip .md5`, dotfiles | ignored |

Existing `.mp3` sources are **copied, not re-encoded** — transcoding lossy to
lossy only throws away quality.

### What MP3 cannot represent

MP3 is a narrow format: 8–48 kHz, mono or stereo only. A lossless library
routinely contains neither.

- **96 kHz / 192 kHz masters** are resampled down to 48 kHz.
- **5.1 and 7.1 sources** are downmixed to stereo.

Both happen automatically, in the encode, with no extra probe. This is the one
real behavioural difference from `rsync2opus` and `rsync2aac`, which keep
surround intact.

### Name collisions

Unlike Opus, MP3 is both an output format and a common input one. An album
directory holding both `track.flac` and `track.mp3` produces two jobs writing
the same destination file. The transcode of the lossless master wins; the lossy
twin is skipped and reported:

```
1 sources collide on a destination path and were skipped:
  Album/05.mp3  (kept Album/05.flac)
```

## Cover art

Embedded in each `.mp3` as a single ID3v2.3 `APIC` front-cover frame: the
source's own attached picture if it has one, otherwise the best
`cover`/`folder`/`front` image sitting in the album directory. Any pre-existing
`APIC` frames are cleared first, since players pick unpredictably between
duplicates.

Tags are written as **ID3v2.3 plus ID3v1** rather than the ffmpeg default of
v2.4 — car stereos and older DAPs routinely ignore v2.4 tags completely.
`--no-art` skips art entirely and is faster.

## Bitrate and VBR

```sh
rsync2mp3 sync src dst              # 256k CBR, the default
rsync2mp3 sync src dst -b 320k      # constant bitrate
rsync2mp3 sync src dst -V 0         # LAME VBR, best quality (~245 kbps)
rsync2mp3 sync src dst -V 2         # LAME VBR, ~190 kbps
```

`-V/--vbr` takes LAME's own quality scale, 0 (best) to 9 (worst), and overrides
`--bitrate`. VBR is smaller than CBR at equal quality; use it unless the target
player is old enough to mistrack VBR files.

`--compression-level` is LAME's `-q`: how hard the encoder looks, 0 (best) to
9 (fastest), default `0`. It changes quality and encoding time, not bitrate.

## Configuration

The shared `config.toml` all three tools read — see
[README.md](README.md#configuration) for the lookup order. Top-level keys apply
everywhere; the `[mp3]` table holds what is MP3-specific:

```toml
source       = "user@host:path/to/music"
max_duration = "25m"
art          = true

[mp3]
dest    = "/path/to/music-mp3"
bitrate = "256k"
# vbr   = 0             # LAME VBR quality, 0-9; overrides bitrate
# compression_level = 0
keep_long = [
  "Sleep/1/01 - Dopesmoker.mp3",
]
```

Command-line flags always override the config. Unknown keys are rejected with
the list of valid ones. `keep_long` entries name **destination** paths, which
is why each codec table spells the same track with its own extension.

## Commands

Four, the same across all three tools:

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

Failures are collected in `.rsync2mp3-failures.log` and never abort the run.

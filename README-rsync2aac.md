# rsync2aac

Mirror a lossless music library into AAC, incrementally, the way rsync mirrors
files. Same tool as [rsync2opus](README.md) — same commands, same config model,
same mtime-based freshness — encoding to AAC in an `.m4a` container.

```sh
rsync2aac sync /music /music-aac
rsync2aac sync nas:/srv/music /music-aac
rsync2aac sync /music user@nas:/srv/music-aac
```

## Why AAC

It is the middle target. Better than MP3 at the same bitrate, worse than Opus,
and the only one of the three Apple hardware plays natively: iPhones, iPods,
CarPlay, older Sonos, most smart TVs and bluetooth receivers. Anything that
reads Opus should get `rsync2opus` and spend half the bitrate.

Measured on the same 2:44 FLAC (874 kbps, 18 MB, 26 tags, embedded art):

| Codec | Settings | Size | Tags kept |
|---|---|---|---|
| Opus | `libopus` 128k VBR | 2.7 MB | 26/26, unchanged |
| MP3 | `libmp3lame` V0 | 6.3 MB | 25/26, remapped to TXXX |
| **AAC** | `aac` 256k | **6.4 MB** | 11/26 natively — **26/26 here**, see below |

### The tag problem, and what this does about it

MP4 has an atom for title, artist, album and about twenty other things, and no
atom at all for ReplayGain, label, catalogue number, ISRC, MusicBrainz ids or
track/disc totals. ffmpeg drops those silently: a well-tagged FLAC library
comes out the far side with less than half its metadata.

So everything without a native atom is written afterwards into iTunes-style
freeform atoms — `----:com.apple.iTunes:REPLAYGAIN_TRACK_GAIN` and friends,
the same place beets and every ReplayGain-aware player look. It costs no extra
work: the probe it needs is the one the cover-art pass already does.
`--no-tags` turns it off.

## Install

```sh
./install.sh          # symlinks all three tools into ~/.local/bin
```

Needs `ffmpeg`, `ffprobe` and `python-mutagen`:

```sh
sudo pacman -S ffmpeg python-mutagen   # Arch
brew install ffmpeg                    # macOS
python3 -m pip install --user mutagen  # macOS: mutagen is not in Homebrew
```

Linux and macOS are both supported, including a macOS box on either end of a
remote sync. Python 3.11 or newer (for `tomllib`).

### Which encoder

`libfdk_aac` is clearly the better AAC encoder, but its licence keeps it out of
every stock ffmpeg build, including Arch's. `--encoder auto` (the default)
takes it when it is there and falls back to ffmpeg's own `aac` encoder when it
is not. `install.sh` says which one you have.

```sh
rsync2aac sync src dst -e aac          # force the native encoder
rsync2aac sync src dst -e libfdk_aac   # fail loudly if it is missing
```

## How files are treated

| Source | Result |
|---|---|
| `.flac .wav .aiff .ape .wv .tta .tak .shn .dsf .dff .w64 .caf` | transcoded to AAC in `.m4a` |
| `.m4a .m4b .mp4` | probed — ALAC/PCM is transcoded, AAC is copied |
| `.mp3 .opus .ogg .aac .mpc .wma` | copied verbatim, never re-encoded |
| `.jpg .png .webp .lrc .cue .m3u .m3u8 .pls .pdf` | copied |
| `.log .nfo .sfv .accurip .md5`, dotfiles | ignored |

Surround **is kept**: AAC-LC carries 5.1 natively, so unlike `rsync2mp3` there
is no downmix. Sample rates above 96 kHz are resampled; everything at or below
it is left alone.

### Name collisions

`.m4a` is both the output container and a common input one, so an ALAC
`track.m4a` transcodes onto its own path, and a directory holding `track.flac`
beside an AAC `track.m4a` produces two jobs writing one destination. The
transcode of the lossless master wins; the lossy twin is skipped and reported:

```
1 sources collide on a destination path and were skipped:
  Mixed/07.m4a  (kept Mixed/07.flac)
```

For the same reason, an `.m4a` that is already current cannot be classified by
its destination path — both ways lead there. Byte count settles it instead: a
copy is exact, a transcode never is. This matters because `reclaim` deletes
transcoded sources and never deletes copied ones.

## Cover art

Embedded in each `.m4a` as the MP4 `covr` atom: the source's own attached
picture if it has one, otherwise the best `cover`/`folder`/`front` image in the
album directory. `covr` carries JPEG and PNG only, so a webp or bmp folder
image is converted to JPEG rather than dropped. `--no-art` skips it and is
faster.

Every encode gets `-movflags +faststart`, which moves the `moov` atom to the
front of the file — streaming players and a good few car head units need that
to start without reading the whole file first.

## Audio quality

```sh
rsync2aac sync src dst              # 192k CBR, the default
rsync2aac sync src dst -b 256k      # constant bitrate
rsync2aac sync src dst -V 5         # VBR, best quality
rsync2aac sync src dst -V 4         # VBR, roughly 192 kbps
```

`-V/--vbr` takes 1 (worst) to 5 (best) and overrides `--bitrate`. That is
libfdk's own scale; with the native encoder it is mapped onto its global
quality setting, so the flag means the same thing either way.

`--bitrate` is the **total** for the file, not per channel — a 5.1 source at
`192k` is thin. Raise it for surround, or use `-V`, which adapts.

## Configuration

Identical to rsync2opus, with its own file:

1. `--config PATH`
2. `./rsync2aac.toml`
3. `$XDG_CONFIG_HOME/rsync2aac/config.toml`, if that variable is set
4. `~/Library/Application Support/rsync2aac/config.toml` (macOS only)
5. `~/.config/rsync2aac/config.toml`

```toml
source = "user@host:path/to/music"
dest   = "/path/to/music-aac"

bitrate = "192k"
# vbr   = 4          # 1-5; overrides bitrate
encoder = "auto"     # auto | libfdk_aac | aac
art     = true
tags    = true       # freeform atoms for ReplayGain, label, ISRC, ...
max_duration = "25m"
```

Command-line flags always override the config. Unknown keys are rejected with
the list of valid ones. `keep_long` entries name **destination** paths, so
carrying a list over from another config means changing the extension to
`.m4a`.

## Commands

Same four as rsync2opus, with the same semantics:

- **`sync`** — convert and copy everything missing or stale. `-n` dry run,
  `-f` force, `--delete` remove orphans, `--adopt` restamp instead of
  re-encoding, `--no-art`, `--no-tags`.
- **`verify`** — full-decode every destination file and compare durations
  against the source. Checks the destination really is AAC.
- **`reclaim`** — verify, then delete source files that are provably mirrored.
  Dry run unless `--yes`.
- **`prune`** — apply a duration limit to an already-synced tree, after the
  fact. Dry run unless `--yes`.

See [README.md](README.md) for the full description of each, plus remote
endpoints, `max_duration`, `keep_long`, and `--adopt`.

Failures are collected in `.rsync2aac-failures.log` and never abort the run.

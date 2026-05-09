#!/usr/bin/env python3
"""Generate a synthetic library tree for documentation screenshots.

Lays down a realistic-looking layout under `--output-dir` that matches the
on-disk schema documented in `Architecture.md`:

    {output-dir}/raw/                 untagged side FLACs
    {output-dir}/in-progress/{slug}/  per-album workspace + album.json manifest
    {output-dir}/music/Artist/Album/  Jellyfin-shaped final tree (tracks)

The FLACs are short ffmpeg sine bursts (~12-30 s) — just enough that the
UI shows non-zero durations / sizes / bit-depth-sample-rate cells. The
album manifests are written out by hand in the v2 schema (see
`app/services/albums_fs.py`) so this script is independent of the running
app: nothing here imports from `app/`.

Usage:
    python tools/seed_demo_data.py --output-dir /tmp/vinyl-shots-output

Re-running clears the chosen output dir first so the seeded library is
always deterministic. The companion screenshot driver
(`tools/screenshots.py`) assumes data already lives at this path; CI is
expected to call this script *before* `screenshots.py`.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import struct
import subprocess
import sys
import time
from pathlib import Path

# A few realistic-looking LP rips — one per row of "Music" / "In-progress".
# Each entry encodes: artist, album, year, genre, label, side count,
# tracklist (per-track titles + duration in seconds; durations are pure UI
# fakes — we don't actually render that many seconds of audio in the side
# FLACs). The audio fed into the side FLACs is short (each side is one
# brief sine burst) so the seed runs in a couple of seconds even with
# ffmpeg encoding to 96 kHz / 24-bit.
RAW_RECORDINGS = [
    # filename_stem, mtime_offset_minutes_ago, duration_seconds, freq_hz
    ("20260507_204512", 35,        24, 196.0),  # most recent first in UI
    ("20260507_201137", 70,        22, 220.0),
    ("20260506_191044", 24*60+12,  18, 261.6),
    ("20260505_223340", 47*60+20,  20, 174.6),
]

# In-progress albums (artist/album/year/genre/label, sides[]) — the wave
# editor pulls these into the split editor screenshot.
IN_PROGRESS_ALBUMS = [
    {
        "slug":   "7f3a8c91",
        "tags": {
            "artist":         "Pink Floyd",
            "album":          "The Dark Side of the Moon",
            "year":           "1973",
            "genre":          "Progressive Rock",
            "label":          "Harvest",
            "catalog_number": "SHVL 804",
            "country":        "UK",
        },
        # Per-side sine frequencies just so the FLAC isn't pure silence.
        "sides": [
            ("20260508_141522.flac", 30, 220.0),  # side A
            ("20260508_142505.flac", 28, 196.0),  # side B
        ],
        # Plan tracks that drive the wave-editor split-editor screenshot.
        # Durations are scaled-down from the real LP runtimes so cuts land
        # WITHIN the seed FLACs' total duration (~58 s = 30 s side-A + 28 s
        # side-B). The editor clips cuts beyond `we.total`, so a real-LP
        # plan would collapse to 1 visible track. Scaled here at ~1/50 to
        # keep the proportions recognisable: Speak/Breathe is short,
        # Time/Money/Us-and-Them are long.
        "plan_tracks": [
            ("Speak to Me / Breathe",       4.7),
            ("On the Run",                  4.3),
            ("Time",                        8.2),
            ("The Great Gig in the Sky",    5.5),
            ("Money",                       7.6),
            ("Us and Them",                 9.2),
            ("Any Colour You Like",         4.1),
            ("Brain Damage",                4.6),
            ("Eclipse",                     5.3),
        ],
        "mtime_minutes_ago": 12 * 60,
    },
    {
        "slug":   "a4d1f2e6",
        "tags": {
            "artist":         "Fleetwood Mac",
            "album":          "Rumours",
            "year":           "1977",
            "genre":          "Rock",
            "label":          "Warner Bros.",
            "catalog_number": "BSK 3010",
            "country":        "US",
        },
        "sides": [
            ("20260506_113040.flac", 26, 246.94),
            ("20260506_115501.flac", 24, 261.63),
        ],
        # Has a draft plan saved (renders as "in progress" — has_draft=True,
        # split=False). Scaled like above.
        "plan_tracks": [
            ("Second Hand News",     6.5),
            ("Dreams",              10.3),
            ("Never Going Back Again", 5.4),
            ("Don't Stop",           7.4),
            ("Go Your Own Way",      8.7),
            ("Songbird",             8.0),
        ],
        "mtime_minutes_ago": 36 * 60,
    },
]

# Already-split albums in music/. Tracks land at music/{Artist}/{Album (Year)}.
MUSIC_ALBUMS = [
    {
        "slug":   "2c8a1b4d",
        "tags": {
            "artist":         "Miles Davis",
            "album":          "Kind of Blue",
            "year":           "1959",
            "genre":          "Jazz",
            "label":          "Columbia",
            "catalog_number": "CL 1355",
            "country":        "US",
        },
        # The split happened — tracks live in music/. We write a tiny FLAC
        # per track so the listing shows real bytes.
        "tracks": [
            ("So What",                 545.0),
            ("Freddie Freeloader",      584.0),
            ("Blue in Green",           337.0),
            ("All Blues",               692.0),
            ("Flamenco Sketches",       563.0),
        ],
        "side_count": 2,
        "mtime_minutes_ago": 5 * 24 * 60,
    },
    {
        "slug":   "9b3e6f01",
        "tags": {
            "artist":         "The Beatles",
            "album":          "Abbey Road",
            "year":           "1969",
            "genre":          "Rock",
            "label":          "Apple Records",
            "catalog_number": "PCS 7088",
            "country":        "UK",
        },
        "tracks": [
            ("Come Together",            259.0),
            ("Something",                182.0),
            ("Maxwell's Silver Hammer",  207.0),
            ("Oh! Darling",              206.0),
            ("Octopus's Garden",         170.0),
            ("Here Comes the Sun",       186.0),
            ("Because",                  166.0),
        ],
        "side_count": 2,
        "mtime_minutes_ago": 8 * 24 * 60,
    },
]

SCHEMA_VERSION = 2  # matches services/albums_fs.SCHEMA_VERSION


def _safe_path_component(s: str) -> str:
    """Mirror of services.ffmpeg.safe_path_component without importing the
    app — keeps this script self-contained."""
    import re
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '', s).strip().rstrip('.')
    return s or 'Unknown'


def _ffmpeg(args: list[str]) -> None:
    """Run ffmpeg with stderr discarded; raise on non-zero."""
    res = subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-y", *args],
        capture_output=True, text=True,
    )
    if res.returncode != 0:
        sys.stderr.write(res.stderr)
        raise RuntimeError(f"ffmpeg failed: {' '.join(args)}")


def _make_sine_flac(
    out: Path, duration_s: float, freq_hz: float,
    sample_rate: int = 96000, bit_depth: int = 24,
) -> None:
    """Render a sine-wave FLAC at the given format. We use ffmpeg's lavfi
    `sine` source so no input file is needed. 24-bit FLAC → encoder uses
    s32 internally; we ask for s32 sample format which metaflac reports
    back as 24."""
    out.parent.mkdir(parents=True, exist_ok=True)
    sample_fmt = "s32" if bit_depth == 24 else "s16"
    _ffmpeg([
        "-f", "lavfi",
        "-i", f"sine=f={freq_hz}:duration={duration_s},volume=0.4",
        "-ar", str(sample_rate),
        "-ac", "2",
        "-sample_fmt", sample_fmt,
        "-c:a", "flac",
        str(out),
    ])


def _set_mtime(p: Path, minutes_ago: float) -> None:
    """Backdate a file's mtime so the UI's "Recorded" column shows a
    realistic spread."""
    target = time.time() - (minutes_ago * 60.0)
    os.utime(p, (target, target))


def _make_cover(out: Path, label: str) -> None:
    """Generate a tiny solid-colour JPEG so the UI's row-thumb / cover-
    preview slots have something to render. Pure stdlib via `struct`-based
    JPEG would be insane; cheat with ffmpeg's `color` source instead."""
    out.parent.mkdir(parents=True, exist_ok=True)
    # Hash the label to a stable colour so each album has a different cover.
    h = sum(ord(c) for c in label)
    r, g, b = 40 + (h * 11) % 180, 30 + (h * 7) % 200, 50 + (h * 13) % 170
    color = f"0x{r:02X}{g:02X}{b:02X}"
    _ffmpeg([
        "-f", "lavfi",
        "-i", f"color=c={color}:s=300x300:d=0.04",
        "-frames:v", "1",
        "-q:v", "5",
        str(out),
    ])


def _write_album_json(album_dir: Path, manifest: dict) -> None:
    album_dir.mkdir(parents=True, exist_ok=True)
    (album_dir / "album.json").write_text(json.dumps(manifest, indent=2))


def _build_plan(track_specs: list[tuple[str, float]], skip_first_lead_in: bool = False) -> dict:
    """Given a list of (title, duration_seconds), build the album.json
    `plan` dict the app expects (see SplitTrack / PlanUpdateRequest)."""
    tracks = []
    if skip_first_lead_in:
        tracks.append({
            "title":            "lead-in",
            "duration_seconds": 2.5,
            "skip":             True,
        })
    for title, dur in track_specs:
        tracks.append({
            "title":            title,
            "duration_seconds": float(dur),
            "skip":             False,
        })
    return {
        "tracks":           tracks,
        "normalize":        True,
        "target_peak_db":   -1.0,
        "measured_peak_db": -3.4,
        "bit_depth":        0,
    }


def seed(output_dir: Path, *, verbose: bool = True) -> None:
    """Wipe + repopulate the output dir with a realistic library."""
    if output_dir.exists():
        # Targeted wipe: only the three subtrees we own. Anything else the
        # caller dropped in (e.g. .logs from a prior run of the live app) is
        # left alone so a manual `OUTPUT_DIR=…` developer isn't surprised.
        for sub in ("raw", "in-progress", "music"):
            d = output_dir / sub
            if d.is_dir():
                shutil.rmtree(d)
    raw_dir = output_dir / "raw"
    inp_dir = output_dir / "in-progress"
    mus_dir = output_dir / "music"
    raw_dir.mkdir(parents=True, exist_ok=True)
    inp_dir.mkdir(parents=True, exist_ok=True)
    mus_dir.mkdir(parents=True, exist_ok=True)

    # ── raw/ — untagged sides waiting to be combined ────────────────────
    for stem, mins_ago, dur, freq in RAW_RECORDINGS:
        p = raw_dir / f"{stem}.flac"
        if verbose:
            print(f"  raw/  {p.name}  ({dur}s @ {freq:.0f} Hz)")
        _make_sine_flac(p, dur, freq)
        _set_mtime(p, mins_ago)

    # ── in-progress/{slug}/ — albums mid-edit (with draft plan) ─────────
    for spec in IN_PROGRESS_ALBUMS:
        d = inp_dir / spec["slug"]
        d.mkdir(parents=True, exist_ok=True)
        sides_filenames: list[str] = []
        for fn, dur, freq in spec["sides"]:
            p = d / fn
            if verbose:
                print(f"  in-progress/{spec['slug']}/{fn}  ({dur}s @ {freq:.0f} Hz)")
            _make_sine_flac(p, dur, freq)
            _set_mtime(p, spec["mtime_minutes_ago"])
            sides_filenames.append(fn)
        # Cover art so the row thumb has something to show.
        cover = d / "cover.jpg"
        _make_cover(cover, spec["tags"]["album"])
        # album.json
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "tags":           dict(spec["tags"]),
            "sides":          sides_filenames,
            "cover":          "cover.jpg",
            "plan":           _build_plan(spec["plan_tracks"], skip_first_lead_in=True),
            "music_relpath":  None,  # not split yet — drives "In-progress"
        }
        _write_album_json(d, manifest)
        _set_mtime(d, spec["mtime_minutes_ago"])

    # ── in-progress/{slug}/ + music/ — already-split albums ─────────────
    for spec in MUSIC_ALBUMS:
        d = inp_dir / spec["slug"]
        d.mkdir(parents=True, exist_ok=True)
        # Stub side FLACs so the album row reports a non-zero duration / size
        # via _summarize_album. We write `side_count` short sines.
        sides_filenames: list[str] = []
        for i in range(spec["side_count"]):
            stem = f"side_{i+1}"
            fn = f"{stem}.flac"
            p = d / fn
            if verbose:
                print(f"  in-progress/{spec['slug']}/{fn}  (side stub)")
            _make_sine_flac(p, 16, 200.0 + 30.0 * i)
            _set_mtime(p, spec["mtime_minutes_ago"])
            sides_filenames.append(fn)
        cover = d / "cover.jpg"
        _make_cover(cover, spec["tags"]["album"])

        # The plan + music_relpath together signal "split has been emitted".
        # _summarize_album reads `music_relpath` to bucket the row into the
        # Music section.
        artist_dir = _safe_path_component(spec["tags"]["artist"])
        year = spec["tags"].get("year", "")
        album_dirname = _safe_path_component(spec["tags"]["album"])
        if year:
            album_dirname = f"{album_dirname} ({year})"
        relpath = f"{artist_dir}/{album_dirname}"
        plan = {
            "tracks":           [
                {"title": title, "duration_seconds": dur, "skip": False}
                for (title, dur) in spec["tracks"]
            ],
            "normalize":        True,
            "target_peak_db":   -1.0,
            "measured_peak_db": -2.7,
            "bit_depth":        0,
        }
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "tags":           dict(spec["tags"]),
            "sides":          sides_filenames,
            "cover":           "cover.jpg",
            "plan":           plan,
            "music_relpath":  relpath,
        }
        _write_album_json(d, manifest)
        _set_mtime(d, spec["mtime_minutes_ago"])

        # Now drop track FLACs into music/{relpath}/. Pad with width=2 to
        # match the app's own emit code in routes/albums.py.
        music_album_dir = mus_dir / relpath
        music_album_dir.mkdir(parents=True, exist_ok=True)
        kept = spec["tracks"]
        pad = max(2, len(str(len(kept))))
        for i, (title, dur) in enumerate(kept, start=1):
            fname = f"{str(i).zfill(pad)} - {_safe_path_component(title)}.flac"
            tp = music_album_dir / fname
            # Render a brief sine — enough for metaflac to read tags / format.
            if verbose:
                print(f"  music/{relpath}/{fname}")
            _make_sine_flac(tp, 12, 200.0 + 5.0 * i)
            _set_mtime(tp, spec["mtime_minutes_ago"])

    if verbose:
        print(f"\nSeeded library at {output_dir}")
        print(f"  raw:         {len(RAW_RECORDINGS)} side(s)")
        print(f"  in-progress: {len(IN_PROGRESS_ALBUMS)} album(s)")
        print(f"  music:       {len(MUSIC_ALBUMS)} album(s)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument(
        "--output-dir", default="/tmp/vinyl-shots-output",
        help="Path used as the recorder's OUTPUT_DIR (default: /tmp/vinyl-shots-output).",
    )
    ap.add_argument(
        "--quiet", action="store_true",
        help="Suppress per-file progress output.",
    )
    args = ap.parse_args()
    out = Path(args.output_dir).expanduser().resolve()
    seed(out, verbose=not args.quiet)
    return 0


if __name__ == "__main__":
    sys.exit(main())

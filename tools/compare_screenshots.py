#!/usr/bin/env python3
"""Pixel-similarity gate for the screenshots workflow.

The CI workflow runs this AFTER `tools/screenshots.py` regenerates the
PNGs and BEFORE the commit-back step. It decides whether to flip the
workflow's `changed` output to `true`. Strict byte equality (`git diff
--quiet`) is the wrong gate here: chromium font-rendering, sub-pixel
anti-aliasing, and PNG encoder timing all produce small byte differences
even when the visible UI is identical, which would otherwise spam the
PR's commit history with no-op screenshot refreshes.

For each PNG under `--directory`:

    * Pull the previously-committed version via `git show HEAD:<path>`.
      If the file is new (no committed predecessor), it counts as
      changed.
    * If the dimensions differ, it counts as changed.
    * Otherwise compute the per-pixel per-channel max difference, count
      the pixels whose max channel difference exceeds `INTENSITY_DELTA`
      (default 5), and compare the fraction against `--threshold`
      (default 0.001 == 0.1 % of pixels).

Emits one line of stdout — `changed=true` or `changed=false` — suitable
for piping straight into `$GITHUB_OUTPUT`. Per-file verdicts go to
stderr so the workflow log is informative without polluting the step
output.

Usage:
    python tools/compare_screenshots.py [--directory DIR] [--threshold F]

Run this from the repo root so `git show HEAD:<path>` resolves.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from io import BytesIO
from pathlib import Path

DEFAULT_THRESHOLD = 0.001  # 0.1 % of pixels
INTENSITY_DELTA   = 5      # per-channel diff below this is encoder noise


def _committed_bytes(rel_path: str) -> bytes | None:
    """Return the bytes of `rel_path` at git HEAD, or None if not committed."""
    r = subprocess.run(
        ["git", "show", f"HEAD:{rel_path}"],
        capture_output=True,
    )
    if r.returncode != 0:
        return None
    return r.stdout


def _fraction_changed(prev_img, cur_img) -> float:
    """Return the fraction (0.0–1.0) of pixels whose max-channel
    difference exceeds INTENSITY_DELTA. Done via Pillow's C-level
    histogram so this stays fast on 1440×900 PNGs."""
    from PIL import ImageChops

    if prev_img.size != cur_img.size:
        return 1.0
    a = prev_img.convert("RGB")
    b = cur_img.convert("RGB")
    diff = ImageChops.difference(a, b)
    # Per-pixel max across R/G/B, computed with two `lighter` ops so the
    # result is the max-channel intensity rather than a luminance-weighted
    # average. Threshold + histogram counts differing pixels in C.
    r, g, bl = diff.split()
    maxch = ImageChops.lighter(ImageChops.lighter(r, g), bl)
    mask  = maxch.point(lambda v: 255 if v > INTENSITY_DELTA else 0)
    differing = mask.histogram()[255]
    total = mask.size[0] * mask.size[1]
    return differing / total


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument(
        "--directory", default="images",
        help="directory of regenerated PNGs to compare against HEAD (default: images)",
    )
    ap.add_argument(
        "--threshold", type=float, default=DEFAULT_THRESHOLD,
        help=f"fraction of pixels that must differ to count as changed (default: {DEFAULT_THRESHOLD})",
    )
    args = ap.parse_args()

    # Lazy import so `--help` doesn't pay the Pillow load cost (and so the
    # script's argparse-only entry path doesn't fail before printing usage
    # if Pillow is missing).
    try:
        from PIL import Image
    except ImportError:
        print("Pillow is required (pip install Pillow)", file=sys.stderr)
        return 2

    d = Path(args.directory)
    pngs = sorted(p for p in d.glob("*.png") if p.is_file())
    if not pngs:
        print(f"no PNGs under {d}/", file=sys.stderr)
        print("changed=false")
        return 0

    any_changed = False
    for p in pngs:
        prev_bytes = _committed_bytes(str(p))
        if prev_bytes is None:
            print(f"  {p}: new file (no predecessor at HEAD) — CHANGED", file=sys.stderr)
            any_changed = True
            continue
        try:
            prev = Image.open(BytesIO(prev_bytes))
            cur  = Image.open(p)
            frac = _fraction_changed(prev, cur)
        except Exception as e:
            print(f"  {p}: comparison failed ({e!r}) — treating as CHANGED", file=sys.stderr)
            any_changed = True
            continue
        verdict = "CHANGED" if frac > args.threshold else "unchanged"
        print(f"  {p}: {frac:.4%} of pixels differ — {verdict}", file=sys.stderr)
        if frac > args.threshold:
            any_changed = True

    print(f"changed={'true' if any_changed else 'false'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

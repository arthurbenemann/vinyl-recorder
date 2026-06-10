"""AcoustID audio-fingerprint identification — "what record is this?".

Pipeline: `fpcalc` (Chromaprint, installed via the Dockerfile) fingerprints
the first ~2 minutes of a recording locally, then one POST to the AcoustID
web service resolves the fingerprint to MusicBrainz release candidates.
The candidates come back in the same compact dict shape as
`musicbrainz.search_releases`, so the tag panel renders them through the
exact same card → `/api/release/{mbid}` → apply flow as a text search.

Sync HTTP via stdlib urllib (same no-deps pattern as the MB/Discogs
clients) so routes can run it on a thread. Requires `ACOUSTID_API_KEY`
(free for non-commercial use: https://acoustid.org/new-application);
`enabled()` gates the whole feature.
"""
import json
import logging
import subprocess
import urllib.parse
import urllib.request
from pathlib import Path

from state import ACOUSTID_API_KEY, ACOUSTID_BASE, MB_UA

logger = logging.getLogger("acoustid")

# Two minutes of audio is far more than AcoustID needs for a confident
# match (their own client defaults to 120 s) and keeps fpcalc quick on a
# 96 kHz/24-bit side.
FINGERPRINT_SECONDS = 120
_FPCALC_TIMEOUT = 120
_LOOKUP_TIMEOUT = 20
_MAX_CANDIDATES = 8


class AcoustidError(Exception):
    """User-presentable identification failure (fpcalc missing, lookup
    refused, malformed response). The route maps it to a 502/503."""


def enabled() -> bool:
    return bool(ACOUSTID_API_KEY)


def fingerprint(path: Path) -> tuple[float, str]:
    """Run fpcalc on `path`; returns (duration_seconds, fingerprint).

    The duration fpcalc reports is the FULL file duration (the -length cap
    only limits how much audio feeds the fingerprint) — AcoustID wants
    that full duration for matching."""
    cmd = ["fpcalc", "-json", "-length", str(FINGERPRINT_SECONDS), str(path)]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True,
                             timeout=_FPCALC_TIMEOUT)
    except FileNotFoundError:
        raise AcoustidError(
            "fpcalc not found — the chromaprint package is missing from "
            "this image")
    except subprocess.TimeoutExpired:
        raise AcoustidError("fingerprinting timed out")
    if out.returncode != 0:
        raise AcoustidError(
            f"fpcalc failed: {(out.stderr or '').strip()[:200] or 'unknown error'}")
    try:
        data = json.loads(out.stdout)
        return float(data["duration"]), str(data["fingerprint"])
    except (ValueError, KeyError, TypeError) as e:
        raise AcoustidError(f"could not parse fpcalc output: {e}")


def lookup(fp: str, duration: float) -> list[dict]:
    """Resolve a fingerprint to MusicBrainz release candidates.

    POSTs form-encoded (the fingerprint string is several KB — too long
    for a GET query string per AcoustID's own docs)."""
    body = urllib.parse.urlencode({
        "client":      ACOUSTID_API_KEY,
        "duration":    str(int(duration)),
        "fingerprint": fp,
        # `releases` carries the release MBIDs + titles/dates/track counts
        # the tag panel needs; `recordings` carries the artist credit.
        "meta": "recordings releases",
    }).encode()
    req = urllib.request.Request(
        ACOUSTID_BASE + "/lookup", data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "User-Agent": MB_UA},
    )
    try:
        with urllib.request.urlopen(req, timeout=_LOOKUP_TIMEOUT) as r:
            data = json.loads(r.read())
    except AcoustidError:
        raise
    except Exception as e:
        raise AcoustidError(f"AcoustID lookup failed: {e}")
    if data.get("status") != "ok":
        msg = (data.get("error") or {}).get("message") or "unknown error"
        raise AcoustidError(f"AcoustID error: {msg}")
    return map_candidates(data)


def map_candidates(data: dict, limit: int = _MAX_CANDIDATES) -> list[dict]:
    """Flatten an AcoustID lookup payload into tag-panel candidate dicts.

    Shape mirrors `musicbrainz.search_releases` so the UI reuses the same
    card renderer and `/api/release/{mbid}` click-through. One release can
    appear under several matched recordings — dedup by MBID, keeping the
    best score and back-filling artist/year/track_count from whichever
    occurrence carries them. Score is AcoustID's 0..1 match confidence,
    scaled to the 0..100 integer the MB cards already display."""
    best: dict[str, dict] = {}
    for result in data.get("results") or []:
        score = int(round(float(result.get("score") or 0.0) * 100))
        for rec in result.get("recordings") or []:
            artists = rec.get("artists") or []
            artist = (artists[0].get("name") or "") if artists else ""
            for rel in rec.get("releases") or []:
                mbid = rel.get("id")
                if not mbid:
                    continue
                date = rel.get("date") or {}
                cand = {
                    "mbid":        mbid,
                    "title":       rel.get("title") or "",
                    "artist":      artist,
                    "year":        str(date.get("year") or ""),
                    "score":       score,
                    "track_count": rel.get("track_count") or 0,
                }
                prev = best.get(mbid)
                if prev is None:
                    best[mbid] = cand
                else:
                    # Keep max score; fill any blanks the earlier
                    # occurrence left.
                    prev["score"] = max(prev["score"], score)
                    for k in ("title", "artist", "year"):
                        if not prev[k] and cand[k]:
                            prev[k] = cand[k]
                    if not prev["track_count"] and cand["track_count"]:
                        prev["track_count"] = cand["track_count"]
    ranked = sorted(best.values(),
                    key=lambda c: (-c["score"], c["title"], c["mbid"]))
    return ranked[:limit]

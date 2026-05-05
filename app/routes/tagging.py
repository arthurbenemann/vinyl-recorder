"""Tagging workflow: MB+Discogs search, release detail, cover proxy, apply."""
import asyncio
import re
import subprocess
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from services import discogs
from services.ffmpeg import (
    find_file, move_to, read_tags, rename_to_match_tags, write_tags,
)
from services.musicbrainz import (
    _http_bytes, caa_front, extract_discogs_id, release_full, search_releases,
)
from state import ApplyRequest, SearchRequest, TAGGED_DIR

router = APIRouter()


@router.post("/api/search")
async def search(req: SearchRequest):
    """Search MusicBrainz for release candidates matching artist+album."""
    if not req.artist.strip() and not req.album.strip():
        return {"candidates": []}
    try:
        candidates = await asyncio.to_thread(search_releases, req.artist.strip(), req.album.strip(), 5)
    except Exception as e:
        raise HTTPException(502, f"MusicBrainz error: {e}")
    return {"candidates": candidates}


@router.get("/api/release/{mbid}")
async def release_detail(mbid: str):
    """Fetch full release details for a chosen MB candidate, enriching with
    Discogs (catalog#, country, format, genres) when MB has linked the two."""
    if not re.fullmatch(r"[0-9a-f-]{36}", mbid):
        raise HTTPException(400, "invalid mbid")
    try:
        mb = await asyncio.to_thread(release_full, mbid)
    except Exception as e:
        raise HTTPException(502, f"MusicBrainz error: {e}")

    artist = (mb.get("artist-credit") or [{}])[0].get("name", "")
    title  = mb.get("title", "")
    year   = (mb.get("date") or "")[:4]
    label  = ""
    catno  = ""
    li = mb.get("label-info") or []
    if li:
        label = (li[0].get("label") or {}).get("name", "") or ""
        catno = li[0].get("catalog-number", "") or ""
    country = mb.get("country", "") or ""
    fmt = (mb.get("media") or [{}])[0].get("format", "") or ""
    tracks: list[str] = []
    track_details: list[dict] = []
    for media in mb.get("media", []):
        for tr in media.get("tracks", []):
            t = tr.get("title") or (tr.get("recording") or {}).get("title", "")
            if not t:
                continue
            tracks.append(t)
            length_ms = tr.get("length") or (tr.get("recording") or {}).get("length")
            track_details.append({
                "title":            t,
                "duration_seconds": (length_ms / 1000.0) if length_ms else None,
            })

    discogs_id = extract_discogs_id(mb)
    genres: list[str] = []
    discogs_url = None
    if discogs_id:
        d = await asyncio.to_thread(discogs.release, discogs_id)
        if d:
            discogs_url = d.get("uri") or f"https://www.discogs.com/release/{discogs_id}"
            # Discogs fields are often more vinyl-accurate; prefer them when set.
            if d.get("labels"):
                d_lbl = d["labels"][0]
                label = d_lbl.get("name") or label
                catno = d_lbl.get("catno") or catno
            country = d.get("country") or country
            if d.get("formats"):
                f0 = d["formats"][0]
                parts = [f0.get("name", "")]
                parts += f0.get("descriptions", []) or []
                fmt = ", ".join(p for p in parts if p) or fmt
            for g in (d.get("genres") or []):
                if g not in genres: genres.append(g)
            for s in (d.get("styles") or []):
                if s not in genres: genres.append(s)
            # Backfill missing track durations from Discogs's "M:SS" strings.
            d_tracks = [t for t in (d.get("tracklist") or []) if not t.get("type_") or t.get("type_") == "track"]
            for i, td in enumerate(track_details):
                if td["duration_seconds"] is None and i < len(d_tracks):
                    dur = (d_tracks[i].get("duration") or "").strip()
                    if dur:
                        try:
                            mm, ss = dur.split(":")
                            td["duration_seconds"] = int(mm) * 60 + int(ss)
                        except ValueError:
                            pass

    return {
        "mbid":           mbid,
        "title":          title,
        "artist":         artist,
        "year":           year,
        "label":          label,
        "catalog_number": catno,
        "country":        country,
        "format":         fmt,
        "genre":          ", ".join(genres),
        "tracks":         tracks,
        "track_details":  track_details,
        "discogs_id":     discogs_id,
        "discogs_url":    discogs_url,
        "cover_url":      f"/api/cover/{mbid}",
    }


@router.get("/api/cover/{mbid}")
async def cover(mbid: str):
    """Proxy cover art for a release. Tries CAA first, falls back to Discogs primary image."""
    if not re.fullmatch(r"[0-9a-f-]{36}", mbid):
        raise HTTPException(400, "invalid mbid")
    art = await asyncio.to_thread(caa_front, mbid)
    if not art:
        try:
            mb = await asyncio.to_thread(release_full, mbid)
            did = extract_discogs_id(mb)
            if did:
                d = await asyncio.to_thread(discogs.release, did)
                images = (d or {}).get("images") or []
                primary = next((i for i in images if i.get("type") == "primary"), images[0] if images else None)
                if primary and primary.get("uri"):
                    art = await asyncio.to_thread(_http_bytes, primary["uri"])
        except Exception:
            pass
    if not art:
        raise HTTPException(404, "no cover available")
    return StreamingResponse(iter([art]), media_type="image/jpeg")


@router.get("/api/file-cover/{filename}")
async def file_cover(filename: str):
    """Serve cover art for any FLAC in the library/albums dirs. Tries the
    embedded picture first (extracted via metaflac), then falls back to CAA
    via the MUSICBRAINZ_ALBUMID tag if present."""
    src = find_file(filename)
    if not src:
        raise HTTPException(404, "file not found")

    tmp = Path(f"/tmp/cover_{uuid.uuid4().hex[:8]}.bin")
    try:
        r = await asyncio.to_thread(
            subprocess.run,
            ["metaflac", f"--export-picture-to={tmp}", str(src)],
            capture_output=True,
        )
        if r.returncode == 0 and tmp.exists() and tmp.stat().st_size > 0:
            data = tmp.read_bytes()
            return StreamingResponse(iter([data]), media_type="image/jpeg")
    finally:
        try: tmp.unlink()
        except Exception: pass

    mbid = (read_tags(src).get("MUSICBRAINZ_ALBUMID") or "").strip()
    if mbid and re.fullmatch(r"[0-9a-f-]{36}", mbid):
        art = await asyncio.to_thread(caa_front, mbid)
        if art:
            return StreamingResponse(iter([art]), media_type="image/jpeg")

    raise HTTPException(404, "no cover available")


@router.post("/api/apply")
async def apply_tags(req: ApplyRequest):
    """Write the chosen tag set to a file, embed cover art if mbid is given,
    move into tagged/, and rename to match."""
    path = find_file(req.filename)
    if not path:
        raise HTTPException(404, "file not found")

    fields = {k: v for k, v in req.fields.dict().items() if v is not None}
    write_tags(path, fields)

    if req.mbid:
        if not re.fullmatch(r"[0-9a-f-]{36}", req.mbid):
            raise HTTPException(400, "invalid mbid")
        subprocess.run(
            ["metaflac", "--remove-tag=MUSICBRAINZ_ALBUMID",
             f"--set-tag=MUSICBRAINZ_ALBUMID={req.mbid}", str(path)],
            check=False, stderr=subprocess.DEVNULL,
        )
        art = await asyncio.to_thread(caa_front, req.mbid)
        if not art:
            try:
                mb = await asyncio.to_thread(release_full, req.mbid)
                did = extract_discogs_id(mb)
                if did:
                    d = await asyncio.to_thread(discogs.release, did)
                    images = (d or {}).get("images") or []
                    primary = next((i for i in images if i.get("type") == "primary"), images[0] if images else None)
                    if primary and primary.get("uri"):
                        art = await asyncio.to_thread(_http_bytes, primary["uri"])
            except Exception:
                pass
        if art:
            tmp = Path(f"/tmp/cover_{uuid.uuid4().hex[:8]}.jpg")
            tmp.write_bytes(art)
            subprocess.run(["metaflac", "--remove", "--block-type=PICTURE", str(path)],
                           check=False, stderr=subprocess.DEVNULL)
            subprocess.run(["metaflac", f"--import-picture-from={tmp}", str(path)],
                           check=False, stderr=subprocess.DEVNULL)
            try: tmp.unlink()
            except Exception: pass

    new_path = move_to(path, TAGGED_DIR) if read_tags(path).get("ARTIST") else path
    renamed = rename_to_match_tags(new_path) if new_path.parent == TAGGED_DIR else new_path
    return {"ok": True, "filename": renamed.name, "tagged": renamed.parent == TAGGED_DIR}

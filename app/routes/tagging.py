"""Tagging workflow: MB+Discogs search, release detail, cover proxy, apply."""
import asyncio
import io
import re
from typing import Optional

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse

from services import albums_fs, discogs
from services.musicbrainz import (
    _http_bytes, caa_front, extract_discogs_id, release_full, search_releases,
)
from state import (
    ApplyRequest, DISCOGS_TOKEN, DISCOGS_USERNAME, SearchRequest,
)

router = APIRouter()


def _str(d: dict, key: str, default: str = "") -> str:
    """`(d.get(key) or "").strip()` shortened. Falsy values (None, "",
    missing key) collapse to `default`; truthy values are stringified
    and stripped. Used throughout MB/Discogs response parsing where
    upstream JSON often has nullable string fields."""
    v = d.get(key)
    return str(v).strip() if v else default


def _mb_artist_relation(mb_release: dict, rel_type: str) -> str:
    """Return a comma-joined list of artist names from a MusicBrainz release's
    `relations[]` matching `rel_type` (e.g. "conductor"). Empty string when
    none. MB stores release-level artist relations as
    `{type, artist:{name,...}}` once `inc=artist-rels` is requested."""
    names: list[str] = []
    for rel in (mb_release.get("relations") or []):
        if (rel.get("type") or "").lower() != rel_type:
            continue
        name = ((rel.get("artist") or {}).get("name") or "").strip()
        if name and name not in names:
            names.append(name)
    return ", ".join(names)


def _original_year(mb_release: dict) -> str:
    """Year the album first came out, from the MB release-group's
    `first-release-date` (present once `inc=release-groups` is requested). For
    a reissue this is earlier than the pressing's own date; persisted as
    `original_year` and written as ORIGINALDATE so libraries sort reissues by
    when the music was released, not when this copy was pressed."""
    rg = mb_release.get("release-group") or {}
    return (rg.get("first-release-date") or "")[:4]


def _mb_extra_tags(mb: dict) -> dict:
    """Pull the stable MusicBrainz identifiers + release facts a music
    server keys on for reliable matching/grouping. Only non-empty values
    are returned so blank tags never get written.

    - `musicbrainz_releasegroupid` / `musicbrainz_artistid` /
      `musicbrainz_albumartistid` — servers dedup releases and fetch
      artist/album art by these IDs. albumartistid mirrors artistid here:
      a single album-artist credit is the vinyl norm and all we can know
      from the release's primary artist-credit.
    - `media` — the medium format ("Vinyl"), so the library can tell a
      needledrop from a CD rip.
    - `releasetype` — the release-group primary type ("Album"/"Single"/
      "Compilation"/"Live"/…) used for browse grouping."""
    out: dict = {}
    rg = mb.get("release-group") or {}
    if _str(rg, "id"):
        out["musicbrainz_releasegroupid"] = _str(rg, "id")
    if _str(rg, "primary-type"):
        out["releasetype"] = _str(rg, "primary-type")
    ac = mb.get("artist-credit") or []
    artist = (ac[0].get("artist") or {}) if ac else {}
    if _str(artist, "id"):
        out["musicbrainz_artistid"] = _str(artist, "id")
        out["musicbrainz_albumartistid"] = _str(artist, "id")
    media = mb.get("media") or []
    if media and _str(media[0], "format"):
        out["media"] = _str(media[0], "format")
    return out


def _discogs_extra_artists(release: dict, role_prefixes: tuple[str, ...]) -> str:
    """Pull credits from a Discogs release's `extraartists[]` whose role
    starts with any of the given prefixes (lowercased). Discogs uses
    "Composed By" as the canonical credit, occasionally with a bracketed
    qualifier ("Composed By [Original Music]"); prefix-matching catches
    those without including unrelated roles like "Cover [Photography]"."""
    names: list[str] = []
    for ea in (release.get("extraartists") or []):
        role = (ea.get("role") or "").strip().lower()
        if not any(role.startswith(p) for p in role_prefixes):
            continue
        name = (ea.get("name") or "").strip()
        if name and name not in names:
            names.append(name)
    return ", ".join(names)


@router.post("/api/search")
async def search(req: SearchRequest):
    """Search MusicBrainz for release candidates, plus matches from the
    user's Discogs collection when configured. Two parallel result lists so
    the UI can render an "From your collection" section above MB results.

    Accepts either a generic free-text `q` (preferred — the UI search bars
    send this) or a structured `artist` + `album` pair (used when the tag
    panel's left-column fields are filled and we want a precise query)."""
    q = req.q.strip()
    artist = req.artist.strip()
    album  = req.album.strip()
    if not q and not artist and not album:
        return {"candidates": [], "collection_candidates": []}
    try:
        if q:
            candidates = await asyncio.to_thread(search_releases, limit=5, q=q)
        else:
            candidates = await asyncio.to_thread(search_releases, artist, album, 5)
    except Exception as e:
        raise HTTPException(502, f"MusicBrainz error: {e}")
    collection_candidates: list[dict] = []
    if DISCOGS_USERNAME:
        try:
            owned = await asyncio.to_thread(
                discogs.collection_releases, DISCOGS_USERNAME, DISCOGS_TOKEN or None,
            )
            if q:
                collection_candidates = await asyncio.to_thread(
                    discogs.match_collection_q, q, owned,
                )
            else:
                collection_candidates = await asyncio.to_thread(
                    discogs.match_collection, artist, album, owned,
                )
        except Exception:
            # Non-fatal: tagging still works without collection enrichment.
            collection_candidates = []
    return {
        "candidates":            candidates,
        "collection_candidates": collection_candidates,
    }


@router.get("/api/collection")
async def collection_list():
    """Return the user's owned Discogs releases, served from the in-process
    cache (refresh via /api/collection/refresh). Used by the tag panel's
    "filter your collection" input so filtering is instant + offline."""
    if not DISCOGS_USERNAME:
        return {"releases": []}
    try:
        owned = await asyncio.to_thread(
            discogs.collection_releases, DISCOGS_USERNAME, DISCOGS_TOKEN or None,
        )
    except Exception:
        # Same non-fatal posture as /api/search — the tag panel still works
        # without collection enrichment, just without the picker.
        owned = []
    return {"releases": owned}


@router.get("/api/collection/status")
async def collection_status():
    """Collection checklist backing the library's Collection section: every
    owned Discogs release annotated with whether it has been recorded
    (exact discogs_release_id match against the album manifests — see
    discogs.annotate_recorded). Served from the same in-process collection
    cache as /api/collection; a fetch failure with a cold cache degrades to
    an empty list (the UI shows its "unavailable" row) rather than erroring,
    matching the non-fatal posture of the other collection endpoints."""
    if not DISCOGS_USERNAME:
        return {"enabled": False, "total": 0, "recorded": 0, "releases": []}
    try:
        owned = await asyncio.to_thread(
            discogs.collection_releases, DISCOGS_USERNAME, DISCOGS_TOKEN or None,
        )
    except Exception:
        owned = []
    albums = await asyncio.to_thread(albums_fs.list_album_tag_summaries)
    releases = discogs.annotate_recorded(owned, albums)
    return {
        "enabled":  True,
        "total":    len(releases),
        "recorded": sum(1 for r in releases if r["recorded"]),
        "releases": releases,
    }


@router.post("/api/collection/refresh")
async def collection_refresh():
    """Rebuild the in-process Discogs collection cache. The cache TTL is
    1 h normally, but a hot refresh is useful right after the user adds a
    record on Discogs and wants the new title to surface immediately."""
    if not DISCOGS_USERNAME:
        raise HTTPException(409, "DISCOGS_USERNAME is not configured")
    try:
        owned = await asyncio.to_thread(
            discogs.collection_releases, DISCOGS_USERNAME, DISCOGS_TOKEN or None, True,
        )
    except Exception as e:
        raise HTTPException(502, f"Discogs error: {e}")
    return {"count": len(owned)}


@router.get("/api/release/discogs/{release_id}")
async def release_detail_discogs(release_id: int):
    """Fetch a Discogs release by ID and return the same shape as
    `/api/release/{mbid}` so the tag panel can populate identically. Used
    when the user picks a candidate from the Discogs-collection section
    (which may not have a paired MusicBrainz release)."""
    if release_id <= 0:
        raise HTTPException(400, "invalid release id")
    d = await asyncio.to_thread(discogs.release, release_id)
    if not d:
        raise HTTPException(502, "Discogs release fetch failed")
    artists = [a.get("name", "") for a in (d.get("artists") or []) if a.get("name")]
    artist  = ", ".join(artists)
    title   = d.get("title", "") or ""
    year    = str(d.get("year") or "") if d.get("year") else ""
    label   = ""
    catno   = ""
    fmt     = ""
    if d.get("labels"):
        l0 = d["labels"][0]
        label = l0.get("name", "") or ""
        catno = l0.get("catno", "") or ""
    country = d.get("country", "") or ""
    if d.get("formats"):
        f0 = d["formats"][0]
        parts = [f0.get("name", "")] + (f0.get("descriptions") or [])
        fmt = ", ".join(p for p in parts if p)
    genres: list[str] = []
    for g in (d.get("genres") or []):
        if g not in genres: genres.append(g)
    for s in (d.get("styles") or []):
        if s not in genres: genres.append(s)
    tracks: list[str] = []
    track_details: list[dict] = []

    def _walk(tr: dict, parent_position: str = "") -> None:
        # Discogs uses a hierarchical tracklist for multi-part / classical
        # works: a parent row with type_="index" (and a movement title) holds
        # the actual playable parts in `sub_tracks`. Recurse into sub_tracks
        # rather than skipping the parent, otherwise releases like classical
        # symphonies come back with an empty tracklist. Heading rows have no
        # audio and stay skipped.
        if tr.get("type_") == "heading":
            return
        if tr.get("sub_tracks"):
            # Movement parent — sub-tracks often inherit their position from
            # the parent (e.g. parent "A1", subs labelled "i", "ii"). Pass
            # the parent down so each leaf carries the side+track key.
            pos = _str(tr, "position") or parent_position
            for sub in tr["sub_tracks"]:
                _walk(sub, pos)
            return
        t = _str(tr, "title")
        if not t:
            return
        tracks.append(t)
        dur = _str(tr, "duration")
        secs: Optional[float] = None
        if dur:
            try:
                mm, ss = dur.split(":")
                secs = int(mm) * 60 + int(ss)
            except ValueError:
                secs = None
        # `position` is the side+track identifier (e.g. "A1", "B2", "1-01" for
        # multi-disc). The wave editor surfaces it on each cut handle and in
        # the track list so on-screen labels match the physical sleeve.
        position = _str(tr, "position") or parent_position
        track_details.append({
            "title": t,
            "duration_seconds": secs,
            "position": position,
        })

    for tr in (d.get("tracklist") or []):
        _walk(tr)
    images = d.get("images") or []
    primary = next((i for i in images if i.get("type") == "primary"),
                   images[0] if images else None)
    cover_url = (primary or {}).get("uri", "") if primary else ""
    discogs_url = d.get("uri") or f"https://www.discogs.com/release/{release_id}"
    composer  = _discogs_extra_artists(d, ("composed by", "composer"))
    conductor = _discogs_extra_artists(d, ("conductor",))
    return {
        "mbid":           None,
        "title":          title,
        "artist":         artist,
        "year":           year,
        "label":          label,
        "catalog_number": catno,
        "country":        country,
        "format":         fmt,
        "genre":          "; ".join(genres),
        "composer":       composer,
        "conductor":      conductor,
        "tracks":         tracks,
        "track_details":  track_details,
        "discogs_id":     release_id,
        "discogs_url":    discogs_url,
        "cover_url":      cover_url,  # external URL — the frontend can <img src> it
    }


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
    # Release-level artist relations: pick out conductor credits. MB models
    # composer per-work (a separate query per recording), so we leave that
    # to the user / Discogs's release-level extraartists block.
    conductor = _mb_artist_relation(mb, "conductor")
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
                "position":         "",
            })

    discogs_id = extract_discogs_id(mb)
    genres: list[str] = []
    discogs_url = None
    composer = ""
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
            composer = _discogs_extra_artists(d, ("composed by", "composer"))
            if not conductor:
                conductor = _discogs_extra_artists(d, ("conductor",))
            # Backfill missing track durations from Discogs's "M:SS" strings,
            # and pull `position` (A1, B2, …) across so the wave editor can
            # label each cut handle with the matching sleeve track.
            d_tracks = [t for t in (d.get("tracklist") or []) if not t.get("type_") or t.get("type_") == "track"]
            for i, td in enumerate(track_details):
                if i < len(d_tracks):
                    if td["duration_seconds"] is None:
                        dur = _str(d_tracks[i], "duration")
                        if dur:
                            try:
                                mm, ss = dur.split(":")
                                td["duration_seconds"] = int(mm) * 60 + int(ss)
                            except ValueError:
                                pass
                    pos = _str(d_tracks[i], "position")
                    if pos:
                        td["position"] = pos

    return {
        "mbid":           mbid,
        "title":          title,
        "artist":         artist,
        "year":           year,
        "label":          label,
        "catalog_number": catno,
        "country":        country,
        "format":         fmt,
        "genre":          "; ".join(genres),
        "composer":       composer,
        "conductor":      conductor,
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


@router.get("/api/file-cover/{album_id}")
async def album_cover(album_id: str):
    """Serve cover art for an album. Returns `cover.jpg` from the album
    dir if present; otherwise falls back to a CAA fetch via
    `tags.musicbrainz_albumid` from the manifest."""
    if not albums_fs.is_valid_album_id(album_id):
        raise HTTPException(404, "album not found")
    if not albums_fs.album_dir(album_id).is_dir():
        raise HTTPException(404, "album not found")
    cover = albums_fs.cover_path(album_id)
    if cover:
        return FileResponse(str(cover), media_type="image/jpeg")
    manifest = albums_fs.read_manifest(album_id)
    mbid = _str(manifest.get("tags") or {}, "musicbrainz_albumid")
    if mbid and re.fullmatch(r"[0-9a-f-]{36}", mbid):
        art = await asyncio.to_thread(caa_front, mbid)
        if art:
            # Cache for future calls so we don't keep hitting CAA.
            try: albums_fs.write_cover(album_id, art)
            except Exception: pass
            return StreamingResponse(iter([art]), media_type="image/jpeg")
    raise HTTPException(404, "no cover available")


# Cap the accepted upload so a multi-hundred-MB scan can't be embedded into
# every track's FLAC at split time. Generous for a cover (a 1500px JPEG is
# well under 1 MB after the re-encode below).
_MAX_COVER_UPLOAD_BYTES = 12 * 1024 * 1024


def _normalize_cover_jpeg(raw: bytes) -> bytes:
    """Decode an arbitrary uploaded image and re-encode it as a bounded RGB
    JPEG. Opening it through Pillow doubles as validation — a non-image (or a
    truncated/corrupt one) raises, which the caller turns into a 400 — and the
    re-encode strips any non-pixel payload rather than trusting the bytes."""
    from PIL import Image
    img = Image.open(io.BytesIO(raw))
    img = img.convert("RGB")
    # Bound the longest edge: covers get embedded into every track, so a huge
    # source scan would bloat the album many times over.
    img.thumbnail((1500, 1500))
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=90)
    return out.getvalue()


@router.post("/api/file-cover/{album_id}")
async def upload_cover(album_id: str, file: UploadFile = File(...)):
    """Set a user-supplied cover image on an album. Useful for obscure
    pressings that MusicBrainz / CAA and Discogs have no art for. The image is
    re-encoded to a bounded JPEG (cover.jpg), which then flows into the split
    output the same way an auto-fetched cover does. Upload only — the server
    never fetches a user-supplied URL, so there's no SSRF surface."""
    if not albums_fs.is_valid_album_id(album_id):
        raise HTTPException(404, "album not found")
    if not albums_fs.album_dir(album_id).is_dir():
        raise HTTPException(404, "album not found")
    raw = await file.read()
    if not raw:
        raise HTTPException(400, "empty upload")
    if len(raw) > _MAX_COVER_UPLOAD_BYTES:
        raise HTTPException(413, "image too large (max 12 MB)")
    try:
        jpeg = await asyncio.to_thread(_normalize_cover_jpeg, raw)
    except Exception:
        raise HTTPException(400, "not a readable image")
    await asyncio.to_thread(albums_fs.write_cover, album_id, jpeg)
    return {"album_id": album_id, "cover": "cover.jpg"}


async def _fetch_cover_bytes(
    mbid: Optional[str],
    discogs_id: Optional[int],
) -> Optional[bytes]:
    """Resolve cover art from CAA first, falling back to Discogs primary
    image. Returns raw image bytes or None. Used by /api/apply when the
    user has just picked a release; the bytes get written to the album's
    cover.jpg (no FLAC embedding at this stage)."""
    if mbid and re.fullmatch(r"[0-9a-f-]{36}", mbid):
        art = await asyncio.to_thread(caa_front, mbid)
        if art:
            return art
    if discogs_id and discogs_id > 0:
        try:
            d = await asyncio.to_thread(discogs.release, discogs_id)
            images = (d or {}).get("images") or []
            primary = next((i for i in images if i.get("type") == "primary"),
                           images[0] if images else None)
            if primary and primary.get("uri"):
                return await asyncio.to_thread(_http_bytes, primary["uri"])
        except Exception:
            return None
    return None


@router.post("/api/apply")
async def apply_tags(req: ApplyRequest):
    """Apply a chosen tag set. Three modes:

    - `album_id` set: patch the in-progress album's `album.json` tags.
      Optionally fetch cover art via CAA/Discogs and save to cover.jpg.
    - `filename` set: promote a raw side into a new in-progress album with
      these tags (calls `albums_fs.create_album`). Same cover handling.
    - `filenames` set: combine N raw sides into a new in-progress album
      with these tags. Same cover handling.

    Tags never touch FLAC bitstreams here — that only happens at the
    split-emit step."""
    targets = [bool(req.album_id), bool(req.filename), bool(req.filenames)]
    if sum(targets) != 1:
        raise HTTPException(
            400, "supply exactly one of album_id / filename / filenames",
        )

    fields = {k: v for k, v in req.fields.dict().items() if v is not None}
    discogs_id = (
        req.discogs_release_id if req.discogs_release_id and req.discogs_release_id > 0
        else None
    )
    if req.mbid:
        if not re.fullmatch(r"[0-9a-f-]{36}", req.mbid):
            raise HTTPException(400, "invalid mbid")
        fields["musicbrainz_albumid"] = req.mbid
        # Fetch the full release once: it carries the Discogs link, the
        # stable IDs / media / releasetype we persist for the music server,
        # and the release-group's first-release-date (original year).
        # release_full memoizes ~5 min, so this is normally a cache hit from
        # the /api/release/{mbid} call the user just made to pick it.
        try:
            mb = await asyncio.to_thread(release_full, req.mbid)
        except Exception:
            mb = None
        if mb:
            if discogs_id is None:
                did = extract_discogs_id(mb)
                if did:
                    discogs_id = did
            fields.update(_mb_extra_tags(mb))
            oy = _original_year(mb)
            if oy:
                fields["original_year"] = oy
    if discogs_id is not None:
        fields["discogs_release_id"] = discogs_id

    if req.album_id:
        if not albums_fs.is_valid_album_id(req.album_id):
            raise HTTPException(404, "album not found")
        if not albums_fs.album_dir(req.album_id).is_dir():
            raise HTTPException(404, "album not found")
        manifest = albums_fs.read_manifest(req.album_id)
        manifest_tags = dict(manifest.get("tags") or {})
        manifest_tags.update(fields)
        manifest["tags"] = manifest_tags
        albums_fs.write_manifest(req.album_id, manifest)
        album_id = req.album_id
    else:
        # Promote raw side(s) into a new album with these tags. Single-side
        # (filename) and N-side (filenames) flows differ only in the list.
        sides = req.filenames if req.filenames else [req.filename]
        try:
            album_id, _ = albums_fs.create_album(sides, fields)
        except FileNotFoundError as e:
            raise HTTPException(404, str(e))
        except ValueError as e:
            raise HTTPException(400, str(e))

    art = await _fetch_cover_bytes(req.mbid, discogs_id)
    if art:
        try: albums_fs.write_cover(album_id, art)
        except Exception: pass

    return {"album_id": album_id}

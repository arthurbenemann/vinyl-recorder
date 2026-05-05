## v0.4.0 — 2026-05-04

- Update Docker publish workflow to only trigger on version tags (#54)
- Handle Discogs tracks that don't fit recording duration (#45)
- Add progress bars for long-running ffmpeg operations (#41)

## v0.3.0 — 2026-05-04

- Test-streams: synthetic audio source for UI testing (#38)
- Tests: add pytest suite (unit + API + e2e) and CI jobs (#39)
- Fix make release CHANGELOG output: drop top heading, fix trailing newline (#53)
- Tests: add crash-recovery + frontend smoke tests (#40)

## 0.2.0 — 2026-05-04

- Server-side connect with shared recording / VU / log state over WebSocket; each tab gets its own local mute (#36).
- Mute / unmute reliability: MP3 proxy, per-subscriber queues, ffmpeg-first cleanup, URL locked while connected (#49).
- Single shared upstream pull so recording no longer kicks the live-listen feed off the Pi (#36).
- Sidebar VU meters: sticky per-channel `CLIP` badge that latches near 0 dBFS, click to clear (#32).
- Header shows app version (git-describe); disk-free marker turns red < 2 GB and blocks record/combine/split (#34).
- Library + Albums: sortable `Fmt` column showing source bit depth / sample rate (#33).
- Library: per-row "promote to album" action for one-take rips (#43).
- Album combine: bulk-select multiple sides into a single tagged FLAC, with `Length` column and aligned tables (#9).
- Unified album-split editor: waveform + audio, snap-to-silence, zoom/pan, suggest popovers, plus tier 1–4 library polish (sticky headers, inline rename, drag reorder, toasts, mobile sidebar, pause/resume) (#13).
- Wave editor: peak/noise/DR readout, album-wide peak normalization, 16/24-bit output dropdown (#29).
- Wave editor: per-region skip toggle (`⊘` / `s`) excluded from output and peak/noise measurement (#29).
- Wave editor: silence detect auto-marks long silences as skip regions; configurable threshold (#29).
- Wave editor: cuts / titles / skip flags persist to `localStorage` per album until the split succeeds (#29).
- Wave editor: skip honored during free playback so the playhead jumps over disabled regions (#44).
- Wave editor: deeper zoom range with skip/silence overlays clipped to the viewport (#47).
- Wave editor: time readouts to a tenth of a second (`m:ss.ss`) (#42).
- Album split: re-encode tracks (not `-c copy`) and pre-load existing cuts when reopening (#26).
- Album split: fix empty Discogs *suggest* when MusicBrainz returns nothing (#27).
- Album split: right-anchor silence and Discogs popovers to stop clipping off-screen (#25).
- Split tracks list: inline play button and size in the expanded row (#48).
- Modals (combine, tag, split) close consistently on `Esc` (#28).
- Drop unused `beets` from the Docker image and config tree.
- Add MIT license and this changelog (#18).
- Library: sortable columns, new `Recorded` column, drop redundant FLAC badge and dead split-by-silence button (#7).
- Pi stream hub: one Pi stream fanned out to browser proxy + recorder, with cached WAV header for late subscribers (#6).
- Two-column tag panel with Discogs enrichment on top of MusicBrainz (#2).

## 0.1.0 — 2026-04-29
- Initial release: record/stop with VU meters, MusicBrainz auto-tagging with cover art, rename-on-tag, configurable default stream, Docker Compose setup.
- Pi capture service with remote ADC gain control and FastAPI stream proxy.
- GitHub Actions CI/CD (#1).

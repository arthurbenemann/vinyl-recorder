## v1.11.0 — 2026-06-12

### Features
- Clickable version pill opens changelog with update indicator (#90)
- Sortable column headers in the Collection section (#91)

## v1.10.0 — 2026-06-11

### Features
- Collection checklist — see which Discogs records still need recording (#89)

## v1.10.0 — 2026-06-11

### Features
- Collection checklist — see which Discogs records still need recording (#89)

## v1.9.0 — 2026-06-11

### Features
- Trigger Jellyfin library scan after successful split (#85)
- Identify records by audio fingerprint (Chromaprint + AcoustID) (#87)
- Armed auto-record — start recording on signal (self-disarms after 24 h) (#86)

## v1.8.0 — 2026-06-03

### Features
- **wave-editor:** Pre/post-silence buffer zones with inward magnets (#84)

## v1.7.0 — 2026-06-01

### Features
- **wave-editor:** Show estimated noise floor next to peak in stats line (#81)
- Background split encoding — survives modal close (#77) (#83)

### Bug Fixes
- P key jump to exact cut, drag scroll, album size for locked albums (#74, #76, #78, #80) (#82)

## v1.6.0 — 2026-05-31

### Features
- **wave-editor:** Drag-to-pan and drag handles to reorder track labels (#72)
- **peaks:** Upgrade to 16-bit dat format; dB-based noise-floor slider (#73)

### Bug Fixes
- **wave-editor:** Spread Discogs tracklist tracks per side, seed lead-in (#71)
- **wave-editor:** Widen actions column (#67), grow + compact wave-editor split view (#66) (#70)

## v1.5.0 — 2026-05-28

### Features
- **ui:** Add tooltips for shortcuts, health stats, peak meter; rename combine button (#22)
- **upstream:** Prompt user to reconnect when the upstream stream drops (#32)
- **ui:** Toast-based undo for deletes, saving indicator, bulk progress, cleaner pi-deploy errors (#27)
- **ui:** Distinct dot/status for idle-standby and paused connection states (#34)
- **ui:** Add a first-run onboarding overlay explaining the pipeline (#37)
- **tags:** Write ALBUMARTIST on every track + COMPILATION for Various Artists (#38)
- **tags:** Write ReplayGain track + album gain on FLAC split output (#39)
- **split:** Write a folder cover.jpg next to the tracks (#47)
- **recording:** Show estimated recording headroom next to disk-free (#50)
- **recording:** Warn when recording but no input signal is detected (#51)
- **wave-editor:** Remember silence-detection settings across albums and reloads (#55)
- **tagging:** Paste a MusicBrainz release link or MBID to load it directly (#56)
- **ui:** "/" focuses the library search (#64)
- **library:** Download a finished album as a single zip (#65)
- **tags:** Write one GENRE tag per value (multi-valued genres) (#40)
- **tags:** Embed MusicBrainz IDs + MEDIA + RELEASETYPE on FLAC tracks (#41)
- **wave-editor:** Keyboard split workflow — `c` adds a cut, ←/→ nudge the nearest (#46)
- **tags:** Write ARTISTSORT / ALBUMARTISTSORT so "The …" artists sort right (#42)
- **wave-editor:** Preview the nearest cut (`p`) to audition a boundary (#48)
- **workflow:** "& edit" — create an album and jump straight into the split editor (#62)
- **split:** Dither when downconverting 24-bit captures to 16-bit (#43)
- **wave-editor:** Flag suspiciously short / long tracks in the track list (#49)
- **tagging:** Show track count on release candidates to pick the pressing (#54)
- **split:** Output channel mode — stereo / mono-sum / left / right (#44)
- **wave-editor:** Make "clear cuts" a non-blocking, undoable action (#53)
- **split:** Write a vinyl-rip.log provenance sidecar next to the tracks (#45)
- **wave-editor:** "split evenly" to seed equal-interval cuts on a gapless side (#58)
- **split:** Export an M3U playlist + CUE sheet alongside the tracks (#52)
- **split:** Write DISCNUMBER/DISCTOTAL for multi-LP sets (#60)
- **tags:** Write ORIGINALDATE so reissues sort by original release (#61)
- **wave-editor:** On-demand keyboard + mouse shortcuts legend (#63)
- **ui:** Remember the stream URL + offer recent sources (#59)
- **tagging:** Upload a custom cover image for an album (#57)

### Bug Fixes
- **ws:** Narrow except in cleanup; refactor(recordings): extract process lifecycle to service (#24)
- **recordings:** Include paused time correctly when finalizing recording elapsed (#28)
- **recordings:** Offload test-stream subprocess.run to a worker thread (#29)
- **albums-fs:** Clean up partial album dir on create_album failure (#30)
- **albums:** Add plan_version conflict detection to prevent multi-tab clobber (#33)
- **wave-editor:** Coalesce concurrent plan saves to avoid racing POSTs (#31)

### Refactoring
- **css:** Extract design tokens, dedupe rules, audit unused selectors (#26)
- **wave-editor:** Extract timeline-state and audio-manager modules; remove dead code (#25)

### Tests
- Add unit coverage for state, ffmpeg, split_orchestrator (#23)

### Changes
- Merge main into claude/cleanup-feedback-SXdzD: keep dirty-pre-fetch (#31) + saving indicator (#27)

## v1.7.0 — 2026-06-01

### Features
- **wave-editor:** Show estimated noise floor next to peak in stats line (#81)
- Background split encoding — survives modal close (#77) (#83)

### Bug Fixes
- P key jump to exact cut, drag scroll, album size for locked albums (#74, #76, #78, #80) (#82)

## v1.6.0 — 2026-05-31

### Features
- **wave-editor:** Drag-to-pan and drag handles to reorder track labels (#72)
- **peaks:** Upgrade to 16-bit dat format; dB-based noise-floor slider (#73)

### Bug Fixes
- **wave-editor:** Spread Discogs tracklist tracks per side, seed lead-in (#71)
- **wave-editor:** Widen actions column (#67), grow + compact wave-editor split view (#66) (#70)

## v1.5.0 — 2026-05-28

### Features
- **ui:** Add tooltips for shortcuts, health stats, peak meter; rename combine button (#22)
- **upstream:** Prompt user to reconnect when the upstream stream drops (#32)
- **ui:** Toast-based undo for deletes, saving indicator, bulk progress, cleaner pi-deploy errors (#27)
- **ui:** Distinct dot/status for idle-standby and paused connection states (#34)
- **ui:** Add a first-run onboarding overlay explaining the pipeline (#37)
- **tags:** Write ALBUMARTIST on every track + COMPILATION for Various Artists (#38)
- **tags:** Write ReplayGain track + album gain on FLAC split output (#39)
- **split:** Write a folder cover.jpg next to the tracks (#47)
- **recording:** Show estimated recording headroom next to disk-free (#50)
- **recording:** Warn when recording but no input signal is detected (#51)
- **wave-editor:** Remember silence-detection settings across albums and reloads (#55)
- **tagging:** Paste a MusicBrainz release link or MBID to load it directly (#56)
- **ui:** "/" focuses the library search (#64)
- **library:** Download a finished album as a single zip (#65)
- **tags:** Write one GENRE tag per value (multi-valued genres) (#40)
- **tags:** Embed MusicBrainz IDs + MEDIA + RELEASETYPE on FLAC tracks (#41)
- **wave-editor:** Keyboard split workflow — `c` adds a cut, ←/→ nudge the nearest (#46)
- **tags:** Write ARTISTSORT / ALBUMARTISTSORT so "The …" artists sort right (#42)
- **wave-editor:** Preview the nearest cut (`p`) to audition a boundary (#48)
- **workflow:** "& edit" — create an album and jump straight into the split editor (#62)
- **split:** Dither when downconverting 24-bit captures to 16-bit (#43)
- **wave-editor:** Flag suspiciously short / long tracks in the track list (#49)
- **tagging:** Show track count on release candidates to pick the pressing (#54)
- **split:** Output channel mode — stereo / mono-sum / left / right (#44)
- **wave-editor:** Make "clear cuts" a non-blocking, undoable action (#53)
- **split:** Write a vinyl-rip.log provenance sidecar next to the tracks (#45)
- **wave-editor:** "split evenly" to seed equal-interval cuts on a gapless side (#58)
- **split:** Export an M3U playlist + CUE sheet alongside the tracks (#52)
- **split:** Write DISCNUMBER/DISCTOTAL for multi-LP sets (#60)
- **tags:** Write ORIGINALDATE so reissues sort by original release (#61)
- **wave-editor:** On-demand keyboard + mouse shortcuts legend (#63)
- **ui:** Remember the stream URL + offer recent sources (#59)
- **tagging:** Upload a custom cover image for an album (#57)

### Bug Fixes
- **ws:** Narrow except in cleanup; refactor(recordings): extract process lifecycle to service (#24)
- **recordings:** Include paused time correctly when finalizing recording elapsed (#28)
- **recordings:** Offload test-stream subprocess.run to a worker thread (#29)
- **albums-fs:** Clean up partial album dir on create_album failure (#30)
- **albums:** Add plan_version conflict detection to prevent multi-tab clobber (#33)
- **wave-editor:** Coalesce concurrent plan saves to avoid racing POSTs (#31)

### Refactoring
- **css:** Extract design tokens, dedupe rules, audit unused selectors (#26)
- **wave-editor:** Extract timeline-state and audio-manager modules; remove dead code (#25)

### Tests
- Add unit coverage for state, ffmpeg, split_orchestrator (#23)

### Changes
- Merge main into claude/cleanup-feedback-SXdzD: keep dirty-pre-fetch (#31) + saving indicator (#27)

## v1.5.0 — 2026-05-22

### Features
- **ui:** Add tooltips for shortcuts, health stats, peak meter; rename combine button (#22)
- **upstream:** Prompt user to reconnect when the upstream stream drops (#32)
- **ui:** Toast-based undo for deletes, saving indicator, bulk progress, cleaner pi-deploy errors (#27)
- **ui:** Distinct dot/status for idle-standby and paused connection states (#34)
- **ui:** Add a first-run onboarding overlay explaining the pipeline (#37)
- **tags:** Write ALBUMARTIST on every track + COMPILATION for Various Artists (#38)
- **tags:** Write ReplayGain track + album gain on FLAC split output (#39)
- **split:** Write a folder cover.jpg next to the tracks (#47)
- **recording:** Show estimated recording headroom next to disk-free (#50)
- **recording:** Warn when recording but no input signal is detected (#51)
- **wave-editor:** Remember silence-detection settings across albums and reloads (#55)
- **tagging:** Paste a MusicBrainz release link or MBID to load it directly (#56)
- **ui:** "/" focuses the library search (#64)
- **library:** Download a finished album as a single zip (#65)
- **tags:** Write one GENRE tag per value (multi-valued genres) (#40)
- **tags:** Embed MusicBrainz IDs + MEDIA + RELEASETYPE on FLAC tracks (#41)
- **wave-editor:** Keyboard split workflow — `c` adds a cut, ←/→ nudge the nearest (#46)
- **tags:** Write ARTISTSORT / ALBUMARTISTSORT so "The …" artists sort right (#42)
- **wave-editor:** Preview the nearest cut (`p`) to audition a boundary (#48)
- **workflow:** "& edit" — create an album and jump straight into the split editor (#62)
- **split:** Dither when downconverting 24-bit captures to 16-bit (#43)
- **wave-editor:** Flag suspiciously short / long tracks in the track list (#49)
- **tagging:** Show track count on release candidates to pick the pressing (#54)
- **split:** Output channel mode — stereo / mono-sum / left / right (#44)
- **wave-editor:** Make "clear cuts" a non-blocking, undoable action (#53)
- **split:** Write a vinyl-rip.log provenance sidecar next to the tracks (#45)
- **wave-editor:** "split evenly" to seed equal-interval cuts on a gapless side (#58)
- **split:** Export an M3U playlist + CUE sheet alongside the tracks (#52)
- **split:** Write DISCNUMBER/DISCTOTAL for multi-LP sets (#60)
- **tags:** Write ORIGINALDATE so reissues sort by original release (#61)
- **wave-editor:** On-demand keyboard + mouse shortcuts legend (#63)
- **ui:** Remember the stream URL + offer recent sources (#59)
- **tagging:** Upload a custom cover image for an album (#57)

### Bug Fixes
- **ws:** Narrow except in cleanup; refactor(recordings): extract process lifecycle to service (#24)
- **recordings:** Include paused time correctly when finalizing recording elapsed (#28)
- **recordings:** Offload test-stream subprocess.run to a worker thread (#29)
- **albums-fs:** Clean up partial album dir on create_album failure (#30)
- **albums:** Add plan_version conflict detection to prevent multi-tab clobber (#33)
- **wave-editor:** Coalesce concurrent plan saves to avoid racing POSTs (#31)

### Refactoring
- **css:** Extract design tokens, dedupe rules, audit unused selectors (#26)
- **wave-editor:** Extract timeline-state and audio-manager modules; remove dead code (#25)

### Tests
- Add unit coverage for state, ffmpeg, split_orchestrator (#23)

### Changes
- Merge main into claude/cleanup-feedback-SXdzD: keep dirty-pre-fetch (#31) + saving indicator (#27)

## v1.4.0 — 2026-05-19

### Features
- **docker:** Inherit host timezone for recording date tags (#18)
- **search:** Generic search, Discogs paste, collection picker (#19)

## v1.3.0 — 2026-05-14

### Features
- **wave-editor:** Shift-drag a cut to also shift later cuts (#13)
- **wave-editor:** Sleeve-position labels on cut handles + per-side Discogs apply (#17)

## v1.2.0 — 2026-05-13

### Features
- **albums:** Delete originals for finished albums to free disk (#10)
- **record:** Smoothed-RMS silence detector for vinyl runouts (#9)

### Bug Fixes
- **albums:** Strip non-tag keys from manifest.tags on write (#11)
- **albums:** Expand Music row track list + fit 5-button actions cell (#12)

## v1.1.1 — 2026-05-12

### Features
- **albums:** Surface 'mixed' format when sides disagree (#6)

### Tests
- **e2e:** Cover pi-deploy modal client-side validation (#7)

## v1.1.0 — 2026-05-12

### Features
- Auto-stop recording on extended silence (#1)
- Editable duration cap + trimmed dropdown options (#4)

### Refactoring
- **deps:** Consolidate pins into pyproject [dependency-groups] (#3)

## v1.0.0 — 2026-05-11

### Features
- **split:** Multi-format export — WAV / MP3 / Ogg / AAC / ALAC (#137)
- **tags:** Expose composer + conductor in tag panel and FLAC tags (#136)

### Refactoring
- Extract subprocess teardown, queue retry, and string helpers (#131)
- **albums:** Move domain logic from routes to services (#132)
- **upstream:** Split into probe, lifecycle, and fan-out modules (#134)
- **state:** Wrap recording session state in manager class (#133)
- **frontend:** Split main.js into ES modules (#135)
- **api:** Standardize response shapes and error format (#138)

## v0.9.1 — 2026-05-10

### Bug Fixes
- **ui:** Widen actions column so the delete X stays in frame (#124)

## v0.9.0 — 2026-05-10

### Features
- Combine per-row play + wave-editor sides reorder with cut remap (#106)
- **library:** Clearer format/sample-rate labels and untruncated dates (#114)
- **splits:** Persistent saved indicator and tighter track rows under 1080px (#115)
- **tagging:** Auto-search MusicBrainz on open and surface applied state (#112)
- **library:** Differentiate stages with subtle borders and surface rename affordance (#113)
- **pi:** In-app "deploy to pi" button replaces manual scp/ssh ceremony (#109)
- **library:** A11y pass on actions and headers; collapsed log; failure pills (#117)
- **mobile:** Card layout for library tables under 720px (#116)
- **pi:** Bootstrap python3 + alsa-utils via apt during deploy (#122)
- **ui:** Add vinyl favicon and logo icon (#123)

### Bug Fixes
- **ui:** Align library table headers with locked column widths (#107)
- **library:** Tighten row-action icons and one-side combine label (#110)

### Performance
- Audioop on VU hot path, mtime cache for metaflac probes (#119)
- Demand-driven upstream lifecycle, batch amixer in /info (#121)

## v0.8.1 — 2026-05-09

### Features
- **api:** Rate-limit MusicBrainz, escape Lucene, cache MB+Discogs releases (#99)

### Bug Fixes
- **ui:** Close XSS class via data-fname; reuse row template; pause polling; a11y (#101)
- **stream:** Pi /stream race, monotonic time, eventbus state-event eviction (#100)
- **build:** Drop tracked-file exclusions from .dockerignore (#105)

### Refactoring
- Extract split_album helpers, single-call write_tags, /api/metrics (#104)

### Tests
- Timeouts, xdist, traces, fixture cleanup, dedicated compose project (#103)

## v0.9.0 — 2026-05-09

### Features
- **api:** Rate-limit MusicBrainz, escape Lucene, cache MB+Discogs releases (#99)

### Bug Fixes
- **ui:** Close XSS class via data-fname; reuse row template; pause polling; a11y (#101)
- **stream:** Pi /stream race, monotonic time, eventbus state-event eviction (#100)
- **build:** Drop tracked-file exclusions from .dockerignore (#105)

### Refactoring
- Extract split_album helpers, single-call write_tags, /api/metrics (#104)

### Tests
- Timeouts, xdist, traces, fixture cleanup, dedicated compose project (#103)

## v0.8.0 — 2026-05-09

### Bug Fixes
- **docker:** Keep tracked screenshots in build context so version isn't dirty (#92)
- **library:** Always show in-progress and music tables (#98)

### Refactoring
- **compose:** Minimal default, full menu in .env.example (#95)

### Tests
- Lift unit+api coverage from 50% to 72% (#96)

### Changes
- Pull published image by default, build via dev overlay (#93)

## v0.7.0 — 2026-05-08

### Features
- **albums:** Faster waveform scrub, audiowaveform dat generated server size, client-rendered waveform (#79)

### Bug Fixes
- **wave-editor:** Dirty-gate auto-save + saved-flash + global pageerror trap (#82)

### Refactoring
- **albums:** Per-side .peaks.dat + side-swap playback, no concat.flac (#88)

### Tests
- **library:** Pin partial-plan merge, demote-preserves-music, has_draft, safe_path (#81)

### Changes
- One workflow for raw → in-progress (combine + tag merged, single-side ok) (#85)
- Reject combine/promote while a recording is in progress (#87)

## v0.6.0 — 2026-05-07

### ⚠ Breaking Changes
- **library:** Album-as-folder workflow (raw → in-progress → music) with demote (#71)

### Features
- **ui:** Collapsible recording sidebar with persistent state (#78)

### Bug Fixes
- Total gaps never zero on clean connection (#73) (#74)

## v0.5.1 — 2026-05-06

### Features
- **docker:** Add healthcheck and /health endpoint (#72)

## v0.5.0 — 2026-05-06

### Features
- **recording:** Pre-roll buffer (default 5s) (#55)
- **stream:** Health metrics + traffic-light indicator (#59)
- **library:** Search & filter (#56)
- **tagging:** Discogs collection-aware suggestions (#57)
- UI polish — sortable albums, bulk promote, progress bars, error handling (#69)

### Bug Fixes
- **docker:** Publish 8080 via default bridge so WebUI works on Docker Desktop (#70)

### Tests
- **e2e:** Poll for VU mask drop instead of fixed 1.5 s sleep (#66)
- **e2e:** Cut wall time by shortening sleeps and dropping host ffmpeg dep (#68)

### Changes
- (fix) Generate /loop stream live with lavfi instead of pre-rendered WAV (#64)
- Shrink image with python:slim base + apt ffmpeg (#62)
- Shrink image with python:alpine base + apk ffmpeg (#63)
- **cliff:** Group changelog by conventional commit type (#67)

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

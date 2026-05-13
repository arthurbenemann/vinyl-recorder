# Side-switch hints + clearer track row — UI previews

Detached screenshot branch for PR #14 (`feat(wave-editor): side-switch
hints on split modal + clearer track row`). Captured headlessly against
a local uvicorn at 1440 × 900, device-scale 2.

Source album: the seeded "Pink Floyd — The Dark Side of the Moon"
(2 sides: 30 s + 28 s = 58 s total).

| state | file |
|-------|------|
| recording side-switch (dashed-blue "Side 2" badge at 0:30) on the waveform + minimap; track list shows the new row-1 "—" marker and the "↦ length" prefix | `side-switches.png` |
| after applying a Discogs tracklist with A1/A2/A3/B1/B2/B3/B4 positions: dotted-orange Discogs side break appears at ~0:22 (A→B), distinct from the recording's side switch at 0:30 | `discogs-side-breaks.png` |

The Discogs tracklist in the second frame is mocked client-side via
`_weApplyTracklist(...)` so the screenshot doesn't depend on a live
Discogs API key. Cuts, titles, and side-break positions are real
output from the live handler.

This branch shares no history with `main` — the `images/*.png` refresh
workflow does not touch it.

# Testing capabilities

The WebUI is end-to-end testable from this repo — do not assume browser
behavior is unverifiable.

## Playwright e2e suite
- Location: `tests/e2e/` (`test_recordings_ui.py`, `test_frontend.py`,
  `test_wave_editor.py`, plus `conftest.py`)
- Drives the live UI at http://localhost:8080
- Use these tests to validate frontend refactors, not just `pytest` on
  Python code

## Fake-stream test fixtures
- `docker-compose.test.yml` overlays `test-streams` (a synthetic audio
  source container) on top of dev compose
- The recorder is pointed at `http://test-streams:8090/loop` via
  `DEFAULT_STREAM_URL`, with `AUTO_CONNECT=true` and
  `PRE_ROLL_SECONDS=0` so timing assertions are reproducible
- Available stream paths from `test-streams`:
  - `/loop` — continuous 440/660 Hz tones (~-8 dBFS); steady VU, basic
    recording, multi-tab sync
  - `/album` — 4 tones, 2 s gaps, 20 s side-break; wave-editor split,
    silence detection, auto-skip
  - `/clip` — clipping content for clip-indicator tests

## How to run
- `make test` — full stack (compose up, run tests, tear down)
- For exploratory runs: `docker compose -f docker-compose.yml -f
  docker-compose.dev.yml -f docker-compose.test.yml up --build -d`,
  then drive Playwright manually or open the UI at localhost:8080

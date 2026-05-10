# Contributing

## Local development

The tracked [docker-compose.yml](docker-compose.yml) pulls the published
image so end users don't need this repo. To run the recorder against the
code in your checkout, layer
[docker-compose.dev.yml](docker-compose.dev.yml) on top — `make` does this
for you:

```bash
make            # docker compose -f docker-compose.yml -f docker-compose.dev.yml up --build -d
```

### Test streams ([test-streams/](test-streams/))

For exercising the recorder UI without a real Pi, an opt-in compose overlay
spins up a synthetic audio source alongside the recorder. It stacks on top
of the dev overlay so the recorder is still built from your checkout:

```bash
make test            # adds -f docker-compose.test.yml on top of the dev stack
```

Then open <http://localhost:8080>. The overlay also overrides the default
host networking with a private bridge network, so the same command works on
Linux, macOS, and Windows.

Stop the stack with `make test-down`; tail logs with `make test-logs`.

The `test-streams` container serves three pre-rendered 96 kHz / 24-bit
stereo WAVs on its `:8090`, looped forever, and `DEFAULT_STREAM_URL` is
wired to `/loop`. Switch streams from the UI's "Stream source" field to hit
the others:

| Path     | What it is                                        | What it tests                                                    |
| -------- | ------------------------------------------------- | ---------------------------------------------------------------- |
| `/loop`  | 60 s of 440 Hz on L + 660 Hz on R at ~−8 dBFS     | VU meter, basic recording, multi-tab connect/disconnect sync     |
| `/album` | 4 tones (30/25/30/25 s) with 2 s gaps + 20 s side break | Wave-editor split, silence detection, auto-skip ≥15 s rule |
| `/clip`  | 50 s sine that intentionally clips for 5 s after a 20 s lead-in | CLIP latch, badge, log line, clip-during-record path |

## Pull request titles

PR titles drive the changelog. Releases run `git-cliff` over the merge
commits on `main` and group them by conventional-commit prefix — so the
prefix you pick determines which section your work shows up in.

Use one of:

| Prefix              | Changelog section | When to use                                       |
|---------------------|-------------------|---------------------------------------------------|
| `feat(scope): …`    | Features          | New user-visible capability                       |
| `feat(scope)!: …`   | ⚠ Breaking Changes | New capability that requires user action to upgrade (data wipe, env var rename, etc.). The `!` works after any prefix — `fix!:`, `refactor!:`, etc. |
| `fix(scope): …`     | Bug Fixes         | Behaviour correction                              |
| `perf(scope): …`    | Performance       | Speed / resource improvements with no API change  |
| `refactor(scope): …`| Refactoring       | Internal restructure, no behaviour change         |
| `test(scope): …`    | Tests             | Test-only PRs                                     |
| `docs: …`           | *(skipped)*       | Documentation                                     |
| `chore: …`          | *(skipped)*       | Tooling, deps, repo housekeeping                  |
| `ci: …`             | *(skipped)*       | CI/CD config                                      |

`scope` is optional but useful — e.g. `feat(tagging):` or `fix(library):`.
Anything outside this list still ends up in the changelog under
**Changes** as a fallback.

### Examples

Good:

- `feat(tagging): persist Discogs release id and surface collection in split menu`
- `feat(library)!: rename output/untagged → output/raw (requires wiping ./output/)`
- `fix(library): keep albums in ALBUMS_DIR when applying tags`
- `perf(stream): cache silencedetect output between split previews`

Avoid:

- `feat(tagging)` — no description; the bullet ends up as just
  `Feat(tagging)` (#N), which isn't useful to anyone reading the release
  notes.
- `update tagging code` — no prefix; lands under generic **Changes**.

### Skipping the changelog

Append `[skip-changelog]` to the PR title (after the conventional prefix
+ subject) if a PR genuinely doesn't belong in the release notes (rare —
most things belong somewhere, even if just under Changes). Example:
`docs: bump screenshot [skip-changelog]`. Direct (non-PR) commits to
`main` are skipped automatically — open a docs PR if you need one in
the changelog.

## Pi capture service — manual deploy

The recorder's **deploy to pi…** menu item handles the common case. Use
the manual recipe below for a custom SSH port, an air-gapped Pi, or any
other reason the in-app flow doesn't fit. These are exactly the steps
the in-app deploy button automates.

```bash
# from this repo on your dev machine
scp pi/server.py pi/pi-recorder.service pi@pi-recorder:/tmp/

# on the Pi
ssh pi@pi-recorder
sudo apt-get update
sudo apt-get install -y python3 alsa-utils
sudo mkdir -p /opt/pi-recorder
sudo mv /tmp/server.py /opt/pi-recorder/
sudo mv /tmp/pi-recorder.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now pi-recorder
systemctl status pi-recorder
```

### Verify

For a regular install, just **connect** in the recorder UI — VU meters
will move and the gain slider will appear if `/info` is reachable. Use
the curl probes below when something looks wrong:

```bash
curl http://pi-recorder:8000/info | jq         # gain + wiring state
ffprobe http://pi-recorder:8000/stream         # pcm_s24le / 96000 Hz / stereo
curl -X POST -H 'Content-Type: application/json' \
     -d '{"db": 12}' http://pi-recorder:8000/gain
```

## Releasing

From a clean `main`, either bump the last tag:

```bash
make release patch    # v0.1.0 → v0.1.1
make release minor    # v0.1.0 → v0.2.0
make release major    # v0.1.0 → v1.0.0
```

…or pin an explicit version: `make release VERSION=v0.2.0`.

This renders every merge commit since the previous tag into a new
section at the top of [CHANGELOG.md](CHANGELOG.md) (via
[git-cliff](https://git-cliff.org) and [`cliff.toml`](./cliff.toml)),
commits it, creates an annotated tag on that commit, pushes both, and
publishes a GitHub Release with the same notes (via
[`gh`](https://cli.github.com)).

If you want a new changelog section or to change how a prefix is
grouped, edit [`cliff.toml`](./cliff.toml) and include a sample of the
resulting changelog in the PR description.

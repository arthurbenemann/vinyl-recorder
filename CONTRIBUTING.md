# Contributing

## Pull request titles

PR titles drive the changelog. Releases run `git-cliff` over the merge
commits on `main` and group them by conventional-commit prefix — so the
prefix you pick determines which section your work shows up in.

Use one of:

| Prefix              | Changelog section | When to use                                       |
|---------------------|-------------------|---------------------------------------------------|
| `feat(scope): …`    | Features          | New user-visible capability                       |
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
- `fix(library): keep albums in ALBUMS_DIR when applying tags`
- `perf(stream): cache silencedetect output between split previews`

Avoid:

- `feat(tagging)` — no description; the bullet ends up as just
  `Feat(tagging)` (#N), which isn't useful to anyone reading the release
  notes.
- `update tagging code` — no prefix; lands under generic **Changes**.

### Skipping the changelog

Add `[skip-changelog]` anywhere in the PR title if a PR genuinely
doesn't belong in the release notes (rare — most things belong
somewhere, even if just under Changes). Direct (non-PR) commits to
`main` are skipped automatically — open a docs PR if you need one in
the changelog.

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

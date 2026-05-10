# Security Policy

## Supported Versions

Vinyl Recorder is a small hobbyist project; only the latest released version
receives security fixes. Pin to a tagged image (e.g.
`ghcr.io/arthurbenemann/vinyl-recorder:v0.9.1`) if you need a stable target,
and update when a new release ships.

## Reporting a Vulnerability

If you believe you've found a security issue, please **do not open a public
GitHub issue**. Instead, report it privately via GitHub's
[Security Advisories](https://github.com/arthurbenemann/vinyl-recorder/security/advisories/new)
form. This sends the report directly to the maintainers and lets us
coordinate a fix and disclosure.

When reporting, include:

- A description of the issue and the impact you believe it has
- Steps to reproduce (a minimal proof-of-concept is ideal)
- The version / commit / image tag you tested against
- Any suggested mitigation, if you have one

You can expect an initial acknowledgement within roughly a week. We'll keep
you updated as we investigate and aim to ship a fix or mitigation as soon as
practical given the project's hobbyist scope.

## Scope

This project is intended to be run on a **trusted local network** (typically
alongside a Raspberry Pi streaming source and a Jellyfin server). It exposes
an unauthenticated HTTP/WebSocket UI and shells out to `ffmpeg`, `metaflac`,
and `audiowaveform`. Do **not** expose it directly to the public internet.

In-scope reports include:

- Command injection via filenames, tags, stream URLs, or other user-controlled
  input
- Path traversal that escapes the configured output directory
- Authentication / authorisation bypass on the Pi deploy endpoint
- Container escape or privilege escalation in the published Docker image
- Vulnerabilities in pinned third-party dependencies that are reachable from
  the app

Out of scope:

- Anything that requires the operator to deliberately expose the app to an
  untrusted network without a reverse proxy / auth layer
- Denial of service from malformed audio streams (the app trusts its input
  source by design)
- Issues in `ffmpeg`, `flac`, `audiowaveform`, or other upstream tools — please
  report those to the respective projects

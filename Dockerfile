# Resolve the app version from .git in the build context. A throwaway stage
# keeps git out of the runtime image; falls back to "dev" if .git is missing
# (e.g. building from a release tarball).
FROM alpine/git AS version
WORKDIR /repo
# Copy the full build context so `git describe --dirty` reflects host-side
# uncommitted changes. core.autocrlf=input normalizes CRLF→LF when diffing,
# so Windows checkouts (which smudge LF→CRLF on the host) don't read as dirty
# inside the Linux build.
COPY . /repo
RUN (git update-index --refresh >/dev/null 2>&1 || true) \
    && git -c core.autocrlf=input describe --tags --always --dirty > /VERSION 2>/dev/null \
    || echo "dev" > /VERSION


FROM python:3.12-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# --no-install-recommends prevents apt from pulling X11, mesa, doc, and audio
# server packages that ffmpeg's transitively-recommended deps drag in but the
# app never uses. flac is for `metaflac` (used by services/ffmpeg.py).
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    flac \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
    fastapi \
    "uvicorn[standard]" \
    python-multipart

WORKDIR /app
COPY app/ /app/
COPY --from=version /VERSION /app/VERSION

RUN mkdir -p /output

EXPOSE 8080

CMD ["python3", "main.py"]

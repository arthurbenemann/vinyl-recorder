# Resolve the app version from .git in the build context. A throwaway stage
# keeps git out of the runtime image; falls back to "dev" if .git is missing
# (e.g. building from a release tarball).
FROM ubuntu:24.04 AS version
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /repo
# Copy the full build context so `git describe --dirty` reflects host-side
# uncommitted changes. core.autocrlf=input normalizes CRLF→LF when diffing,
# so Windows checkouts (which smudge LF→CRLF on the host) don't read as dirty
# inside the Linux build.
COPY . /repo
RUN (git update-index --refresh >/dev/null 2>&1 || true) \
    && git -c core.autocrlf=input describe --tags --always --dirty > /VERSION 2>/dev/null \
    || echo "dev" > /VERSION


FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y \
    ffmpeg \
    flac \
    python3-pip \
    python3-venv \
    && rm -rf /var/lib/apt/lists/*

RUN pip3 install --break-system-packages \
    fastapi \
    "uvicorn[standard]" \
    python-multipart

WORKDIR /app
COPY app/ /app/
COPY --from=version /VERSION /app/VERSION

RUN mkdir -p /output

EXPOSE 8080

CMD ["python3", "main.py"]

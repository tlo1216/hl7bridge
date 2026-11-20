# syntax=docker/dockerfile:1
#
# hl7bridge container image. Synthetic data only.
#
#   docker build -t hl7bridge:local .
#   docker run --rm -p 127.0.0.1:2575:2575 hl7bridge:local
#
# The listener binds 0.0.0.0 inside the container so the published port
# reaches it. Publish it on 127.0.0.1 only, as above: MLLP has no
# encryption or authentication.
#
# Base images are pinned by digest. To update one, look up the new digest
# for the tag and change both the tag comment and the digest together.

# python:3.12.16-slim-trixie (multi-arch index)
ARG PYTHON_IMAGE=python:3.12.16-slim-trixie@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b
# ghcr.io/astral-sh/uv:0.13.9 (multi-arch index)
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.13.9@sha256:d3b601f0c7b118bc1753febf7c41cc80981df678445e0254284eecf84db79a7d

FROM ${UV_IMAGE} AS uv

# -- build: the wheel, and a hashed requirements file from uv.lock ---------------
# The build backend (hatchling) and its dependencies come from the hashed
# build lock that CI uses too, so the wheel is built with pinned tools.
FROM ${PYTHON_IMAGE} AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_NO_CACHE=1 UV_PYTHON_DOWNLOADS=never
WORKDIR /src
COPY pyproject.toml uv.lock README.md LICENSE NOTICE ./
COPY .github/requirements/build.txt ./.github/requirements/build.txt
COPY src ./src
RUN uv build --wheel --out-dir /dist \
        --build-constraints .github/requirements/build.txt --require-hashes \
    && uv export --frozen --no-dev --no-emit-project --format requirements-txt \
        --output-file /dist/requirements.txt

# -- runtime: the wheel and its locked dependencies, nothing else ---------------
FROM ${PYTHON_IMAGE} AS runtime
ARG VERSION=0.5.0
LABEL org.opencontainers.image.title="hl7bridge" \
      org.opencontainers.image.description="HL7 v2, DICOM and FHIR integration toolkit. Synthetic data only." \
      org.opencontainers.image.source="https://github.com/tlo1216/hl7bridge" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.version="${VERSION}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HL7BRIDGE_DB=/data/hl7bridge.db

COPY --from=build /dist /tmp/dist
RUN python -m pip install --require-hashes --only-binary=:all: -r /tmp/dist/requirements.txt \
    && python -m pip install --no-deps /tmp/dist/*.whl \
    && rm -rf /tmp/dist \
    && groupadd --system --gid 10001 hl7bridge \
    && useradd --system --uid 10001 --gid 10001 --home-dir /home/hl7bridge --create-home \
        --shell /usr/sbin/nologin hl7bridge \
    && mkdir -p /data \
    && chown 10001:10001 /data

# A named volume mounted on /data starts out owned by this user.
VOLUME ["/data"]
WORKDIR /data
USER 10001:10001
EXPOSE 2575

# A TCP connect on the listener port. Commands other than listen (demo,
# doctor) finish long before the first check runs.
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
    CMD ["python", "-c", "import socket; socket.create_connection(('127.0.0.1', 2575), 2).close()"]

ENTRYPOINT ["hl7bridge"]
CMD ["listen", "--host", "0.0.0.0", "--port", "2575"]

# syntax=docker/dockerfile:1

# Python 3.12 is not cosmetic: hyundai-kia-connect-api 4.33.1 declares
# Requires-Python >=3.12, and the working host install runs 3.12.12. The patch
# version is pinned so a Python bump arrives as a reviewable Renovate PR.
FROM python:3.12.12-slim-trixie AS builder

# renovate: datasource=github-releases depName=astral-sh/uv
ARG UV_VERSION=0.12.23

# The upstream collector has no release tags worth tracking and no container
# image of its own, so it is vendored from a pinned commit. Renovate watches
# the default branch and opens a digest PR; that PR is also the moment the
# patches below are re-tested, which is the point of pinning this way.
# renovate: datasource=git-refs depName=https://github.com/ZuinigeRijder/hyundai_kia_connect_monitor currentValue=main
ARG MONITOR_COMMIT=a3744c096db66be077f7b496c96b76f6afc8854d

# Was a second git checkout on the host, at tag v4.33.1. That tree is
# byte-identical to the PyPI distribution of the same version (verified
# 2026-10-04), so the dependency is taken from PyPI and Renovate can track it.
# renovate: datasource=pypi depName=hyundai-kia-connect-api
ARG API_VERSION=4.33.1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl patch \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:${UV_VERSION} /uv /usr/local/bin/uv

# Dependencies. geopy is deliberately absent: reverse geocoding goes to
# Nominatim over plain requests and works without it. gspread and paho-mqtt are
# absent too -- send_to_mqtt and the Google Sheets path are off, and neither
# module is imported at import time.
RUN uv venv /opt/venv \
    && VIRTUAL_ENV=/opt/venv uv pip install --no-cache "hyundai-kia-connect-api==${API_VERSION}"

WORKDIR /src
RUN curl -fsSL "https://codeload.github.com/ZuinigeRijder/hyundai_kia_connect_monitor/tar.gz/${MONITOR_COMMIT}" \
      -o monitor.tar.gz \
    && tar -xzf monitor.tar.gz --strip-components=1 \
    && rm monitor.tar.gz

# Three local fixes that are not upstream. They are applied as patches, with
# -F0 (no fuzz), so that an upstream bump which moves this code FAILS THE BUILD
# instead of silently producing an image without them. One of them is the guard
# that stops the car from being woken; re-vendoring the edited files instead
# would hide exactly the change that needs review.
COPY patches/ /patches/
RUN set -eu; \
    for p in /patches/*.patch; do \
      echo "Applying $(basename "$p")"; \
      patch -p1 -F0 --no-backup-if-mismatch < "$p"; \
    done

# Prove the three fixes are in the built tree, so a patch that applies to the
# wrong place still fails the build.
RUN set -eu; \
    grep -q 'ConfigParser(interpolation=None)' monitor.py; \
    grep -q 'ConfigParser(interpolation=None)' monitor_utils.py; \
    grep -q 'REFUSING TO START' monitor.py; \
    grep -q 'sys.exit(3)' monitor.py; \
    grep -q 'type(ex).__name__' monitor.py

# Only what monitor.py needs at runtime. summary.py, dailystats.py, the
# notebooks and the test suite stay out of the image.
RUN mkdir -p /app \
    && cp monitor.py monitor_utils.py mqtt_utils.py domoticz_utils.py \
          monitor.translations.csv logging_config.ini /app/ \
    && cp LICENSE /app/LICENSE.upstream \
    && echo "${MONITOR_COMMIT}" > /app/UPSTREAM_COMMIT


FROM python:3.12.12-slim-trixie

LABEL org.opencontainers.image.title="hyundai-monitor" \
      org.opencontainers.image.description="IONIQ 5 telemetry collector (hyundai_kia_connect_monitor) in infinite mode" \
      org.opencontainers.image.source="https://github.com/jvhaarst/hyundai-monitor-container" \
      org.opencontainers.image.licenses="Apache-2.0"

# tini: python as PID 1 leaves SIGTERM unhandled, and the kernel ignores
# unhandled signals for PID 1. Without an init the pod would sit out its whole
# termination grace period on every rollout and then be SIGKILLed.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tini \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 1000 --user-group --no-create-home --shell /usr/sbin/nologin monitor

COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /app /app
COPY docker-entrypoint.sh /app/docker-entrypoint.sh

# get_filepath() in monitor_utils.py looks in the working directory first and
# then next to the script. The working directory is the data volume, which must
# never hold credentials, so the assembled config is reached through this
# symlink into a writable tmpfs. /config is an emptyDir in the chart.
RUN chmod +x /app/docker-entrypoint.sh \
    && ln -s /config/monitor.cfg /app/monitor.cfg \
    && mkdir -p /config /data \
    && chown monitor:monitor /config /data

ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MONITOR_CONFIG_DIR=/config \
    MONITOR_SECRETS_DIR=/secrets \
    TZ=Etc/UTC

# The CSVs are appended to forever and are the point of the project; they live
# on the volume mounted here. Working directory, because monitor.py resolves
# monitor.csv, monitor.dailystats.csv, monitor.tripinfo.csv and monitor.lastrun
# relative to it.
WORKDIR /data
VOLUME ["/data"]

USER 1000:1000

ENTRYPOINT ["/usr/bin/tini", "--", "/app/docker-entrypoint.sh"]

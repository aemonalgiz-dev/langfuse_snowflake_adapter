# syntax=docker/dockerfile:1

# Build stage: install the package and its dependencies into a virtualenv.
FROM python:3.12-slim AS build

ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /src
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install .


# Runtime stage: only the virtualenv, run as an unprivileged user.
FROM python:3.12-slim

RUN useradd --system --uid 10001 --create-home app \
    && mkdir /data \
    && chown app:app /data

COPY --from=build /opt/venv /opt/venv

# No credential is built into the image. Pass them when the container starts,
# either as files under /run/secrets named after the setting in lower case
# (langfuse_public_key, langfuse_secret_key, snowflake_private_key,
# sync_api_key) or as environment variables.
#
# The service listens on every interface inside the container, so it refuses
# to start without the access key (sync_api_key). Settings changed in the web
# app are kept in /data; mount a volume there to keep them across replacements.
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    SYNC_API_HOST=0.0.0.0 \
    SYNC_API_PORT=8000 \
    SYNC_CONFIG_FILE=/data/config.json

USER app
WORKDIR /data
VOLUME /data
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('SYNC_API_PORT', '8000') + '/health', timeout=3)"

# `serve` runs the web app, the API and the scheduler. Any other command of the
# CLI works too, e.g. `docker run ... langfuse-to-snowflake sync`.
ENTRYPOINT ["langfuse-to-snowflake"]
CMD ["serve"]

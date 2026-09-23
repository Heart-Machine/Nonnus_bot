# Pinned by digest, not just by tag: `3.13-slim` moves, so without this a
# rebuild of an old commit would land on a different base image. To move it
# forward deliberately, take the digest the registry currently serves:
#
#     docker pull python:3.13-slim
#     docker inspect --format='{{index .RepoDigests 0}}' python:3.13-slim
#
FROM python:3.13-slim@sha256:8d9d0b8bcf6506481eae4907c18f5e3e7902e629f5f6d684f9e7c32e85e3ddf0

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# requirements.txt is the generated lock: every version, including transitive
# ones, is pinned, so this build step resolves to the same thing every time.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .
COPY nonnus/ ./nonnus/
COPY assets/ ./assets/

# Import everything once at build time. A module the COPY lines above miss,
# or an asset path that no longer resolves, then fails the image build - in
# CI, on the pull request - instead of the container on the server.
RUN python -c "import bot, nonnus.app"

RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/data /app/cookies \
    && chown -R appuser:appuser /app

USER appuser

CMD ["python", "bot.py"]

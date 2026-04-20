FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    DEBIAN_FRONTEND=noninteractive

# System deps for Camoufox (Firefox binary needs several graphical + font libs
# even in headless mode). Based on the playwright-firefox deps list since
# Camoufox is a Firefox fork.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl wget xz-utils \
        libasound2 libatk-bridge2.0-0 libatk1.0-0 libcairo2 libcups2 \
        libdbus-1-3 libexpat1 libfontconfig1 libgbm1 libglib2.0-0 \
        libgtk-3-0 libnspr4 libnss3 libpango-1.0-0 libx11-6 libx11-xcb1 \
        libxcb1 libxcomposite1 libxcursor1 libxdamage1 libxext6 libxfixes3 \
        libxi6 libxkbcommon0 libxrandr2 libxrender1 libxshmfence1 libxtst6 \
        fonts-liberation fonts-noto-color-emoji \
        xvfb dumb-init \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python deps — pinned loosely; lockfile can be added later if needed
COPY requirements.txt .
RUN pip install -r requirements.txt

# Download the Camoufox Firefox binary at build time so first request isn't
# waiting on a 200 MB fetch. The cache volume mount in docker-compose lets
# us persist it across rebuilds.
RUN python -m camoufox fetch

# Copy solver source (only src/; keep vendored trees in separate context)
COPY src/ ./src/

EXPOSE 8899

# dumb-init handles signal forwarding to uvicorn correctly
ENTRYPOINT ["dumb-init", "--"]
CMD ["uvicorn", "src.server:app", "--host", "0.0.0.0", "--port", "8899", "--log-level", "info"]

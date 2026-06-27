FROM python:3.11-slim

# Chromium + Xvfb (virtual display required — DataDome blocks headless Chrome).
# 'chromium' is available on both amd64 and arm64 Debian, so this image builds
# natively on Raspberry Pi 4/5 without emulation.
RUN apt-get update && apt-get install -y --no-install-recommends \
    chromium \
    xvfb \
    fonts-liberation \
    fonts-noto-core \
    && rm -rf /var/lib/apt/lists/*

# Tell nodriver where to find the system Chromium binary
ENV CHROME_EXECUTABLE_PATH=/usr/bin/chromium

WORKDIR /app

# Install uv, then sync deps from lockfile (no dev extras)
COPY pyproject.toml uv.lock ./
RUN pip install --no-cache-dir uv && uv sync --no-dev --frozen

COPY flatbot/ ./flatbot/

# Persistent data volume mount point
RUN mkdir -p /app/data

COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh

ENTRYPOINT ["/docker-entrypoint.sh"]

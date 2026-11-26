# syntax=docker/dockerfile:1.7

FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Cache Debian packages between builds.
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    apt-get update && \
    apt-get install -y --no-install-recommends \
        ca-certificates \
        wget \
        xz-utils \
        build-essential \
        libxml2-dev \
        libxslt1-dev \
        libgtk-3-0 \
        libdbus-glib-1-2 \
        libasound2 \
        libx11-xcb1 \
        libxtst6 \
        libxt6 \
        libpci3 \
        fonts-liberation \
        fonts-dejavu && \
    rm -rf /var/lib/apt/lists/*

# Cache pip downloads and installed dependency layers.
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --upgrade pip setuptools wheel

# Download architecture-specific Firefox and cache the archive.
RUN --mount=type=cache,target=/var/cache/novelbot \
    arch="$(dpkg --print-architecture)" && \
    case "$arch" in \
        amd64) MOZ_OS=linux64 ;; \
        arm64) MOZ_OS=linux64-aarch64 ;; \
        *) echo "Unsupported architecture: $arch" && exit 1 ;; \
    esac && \
    archive="/var/cache/novelbot/firefox-$arch.tar.xz" && \
    temp_archive="${archive}.tmp" && \
    if ! tar -tJf "$archive" >/dev/null 2>&1; then \
        rm -f "$archive" "$temp_archive" && \
        wget -O "$temp_archive" \
        "https://download.mozilla.org/?product=firefox-latest&os=$MOZ_OS&lang=en-US" && \
        tar -tJf "$temp_archive" >/dev/null && \
        mv "$temp_archive" "$archive"; \
    fi && \
    rm -rf /opt/firefox && \
    tar -xJf "$archive" -C /opt && \
    ln -sf /opt/firefox/firefox /usr/local/bin/firefox

# Changes here rebuild the crawler installation layer.
COPY lightnovel-crawler-dev/ /app/lncrawl-src/

RUN --mount=type=cache,target=/root/.cache/pip \
    pip install /app/lncrawl-src/

COPY psycopg2-binary==2.9.11.txt .

RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r psycopg2-binary==2.9.11.txt

COPY pyTelegramBotAPI==4.29.1.txt .

RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r pyTelegramBotAPI==4.29.1.txt

COPY requests.txt .

RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requests.txt

# Keep main.py last so normal bot-code changes rebuild only the final layer.
COPY main.py .

RUN mkdir -p /app/novels

CMD ["python", "main.py"]
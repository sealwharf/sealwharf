# Smart-card daemon for the optional "reader" service; only needed for tokens.
FROM debian:bookworm-slim AS reader
RUN apt-get update \
    && apt-get install -y --no-install-recommends pcscd libccid \
    && rm -rf /var/lib/apt/lists/*
CMD ["pcscd", "--foreground"]

FROM python:3.12-slim-bookworm AS app

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOME=/tmp

# LibreOffice converts DOCX to PDF. Liberation, Carlito, Caladea and the URW set
# have the same metrics as Arial, Times New Roman, Calibri, Cambria, Helvetica and
# Palatino, so layouts stay stable; the other fonts are free ones common in
# documents written on the web. fontconfig tells which fonts had to be replaced.
# OpenSC is the default PKCS#11 library for tokens. The token-driver command
# installs vendor libraries; libgdbm-compat4 is what SafeSign needs to load.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libreoffice-writer-nogui fontconfig \
       fonts-liberation fonts-dejavu-core fonts-crosextra-carlito fonts-crosextra-caladea fonts-urw-base35 \
       fonts-noto-core fonts-noto-mono fonts-roboto-unhinted fonts-roboto-slab fonts-open-sans fonts-lato \
       fonts-inter fonts-ebgaramond fonts-comfortaa fonts-lobster fonts-comic-neue \
       opensc-pkcs11 libpcsclite1 libgdbm-compat4 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --user-group --no-create-home sealwharf \
    && mkdir -p /data /config \
    && chown sealwharf:sealwharf /data

COPY docker/fonts.conf /etc/fonts/conf.d/60-sealwharf.conf

WORKDIR /app
COPY config/requirements-lock.txt ./
RUN pip install -r requirements-lock.txt
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-deps .

USER 10001:10001
HEALTHCHECK --interval=30s --timeout=10s --start-period=180s --retries=3 CMD ["python", "-m", "sealwharf.healthcheck"]
ENTRYPOINT ["python", "-m", "sealwharf"]
CMD ["run"]

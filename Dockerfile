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

# LibreOffice converts DOCX to PDF. Liberation, Carlito and Caladea have the same
# metrics as Arial, Times New Roman, Calibri and Cambria, so layouts stay stable.
# OpenSC is the default PKCS#11 library for tokens. The token-driver command
# installs vendor libraries; libgdbm-compat4 is what SafeSign needs to load.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libreoffice-writer-nogui fonts-liberation fonts-dejavu-core fonts-crosextra-carlito fonts-crosextra-caladea opensc-pkcs11 libpcsclite1 libgdbm-compat4 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --user-group --no-create-home sealwharf \
    && mkdir -p /data /config \
    && chown sealwharf:sealwharf /data

COPY docker/fonts.conf /etc/fonts/conf.d/60-sealwharf.conf

# The connector that publishes the webhook at the fixed Cloudflare address.
COPY --from=cloudflare/cloudflared:latest /usr/local/bin/cloudflared /usr/local/bin/cloudflared

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

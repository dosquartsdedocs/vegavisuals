ARG BASE_IMAGE=python:3.13-slim-bookworm@sha256:00faa2debb87529f9f0764e9491d8ba400a3678976616c3bd7cb193745ac20d1
FROM ${BASE_IMAGE}

ARG VL_CONVERT_VERSION=1.9.0.post1
ARG QPDF_VERSION=11.3.0-1+deb12u1

LABEL org.opencontainers.image.title="vegavisuals renderer" \
      org.opencontainers.image.description="Offline Vega-Lite and Vega renderer" \
      org.opencontainers.image.source="https://github.com/dosquartsdedocs/vegavisuals" \
      org.opencontainers.image.licenses="GPL-3.0-only" \
      io.context.mcp-factory="vegavisuals"

ENV DEBIAN_FRONTEND=noninteractive \
    HOME=/tmp \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONHASHSEED=0 \
    SOURCE_DATE_EPOCH=0 \
    TZ=UTC \
    XDG_CACHE_HOME=/tmp/.cache

RUN apt-get update \
    && apt-get install -y --no-install-recommends fontconfig=2.14.1-4 fonts-dejavu-core=2.37-6 "qpdf=${QPDF_VERSION}" \
    && rm -rf /var/lib/apt/lists/* \
    && python -m pip install --no-cache-dir --disable-pip-version-check "vl-convert-python==${VL_CONVERT_VERSION}" \
    && fc-cache -f \
    && python -c "import importlib.metadata, vl_convert; assert importlib.metadata.version('vl-convert-python') == '${VL_CONVERT_VERSION}'; assert vl_convert.get_vega_version() == '6.2.0'" \
    && qpdf --version | grep -F 'qpdf version 11.3.0'

COPY docker/worker.py /opt/vegavisuals/worker.py
COPY themes/ /opt/vegavisuals/themes/

WORKDIR /output
ENTRYPOINT ["python", "/opt/vegavisuals/worker.py"]

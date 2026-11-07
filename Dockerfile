# Controller image (DESIGN.md 11.3, ADR 0019). Contains only controller/: the simulator package is
# deliberately absent, so the image itself proves the controller never imports `sim`.
#
# Reproducible: the base image is pinned by its multi-arch index digest and every Python package by
# version and hash (requirements-runtime.lock, no pytest). run.sh passes PYTHON_BASE only when the
# pinned digest cannot be obtained and the plain tag can.
ARG PYTHON_BASE=python:3.11-slim@sha256:65bd3d9f1d7fa268dd1909973ba7eb4bdf41f6dcf2cfbe76ba9528b170f3fd79
FROM ${PYTHON_BASE}

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 PIP_ROOT_USER_ACTION=ignore

WORKDIR /app
COPY requirements-runtime.lock ./
# Vendored wheels (scripts/vendor_wheels.sh) make the build work without PyPI; without them the
# build installs the same locked files from the index (PIP_INDEX_URL for a mirror).
COPY vendor/wheels/ /tmp/wheels/
ARG PIP_INDEX_URL=https://pypi.org/simple
RUN set -e; \
    if ls /tmp/wheels/*.whl >/dev/null 2>&1; then source="--no-index --find-links /tmp/wheels"; \
    else source="--index-url ${PIP_INDEX_URL} --retries 5 --timeout 60"; fi; \
    pip install $source --require-hashes --no-deps --only-binary=:all: -r requirements-runtime.lock; \
    rm -rf /tmp/wheels; \
    python -c "import fastapi, httpx, numpy, psycopg, psycopg_pool, uvicorn, yaml"

COPY controller/ controller/
ARG CODE_VERSION=dev
ENV CTL_CODE_VERSION=${CODE_VERSION}
USER nobody
ENTRYPOINT ["python", "-m", "controller"]

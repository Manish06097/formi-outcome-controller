# Controller image (DESIGN.md 11.3, ADR 0019). Contains only controller/: the simulator package is
# deliberately absent, so the image itself proves the controller never imports `sim`.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY requirements.txt requirements-controller.txt ./
# Vendored wheels (scripts/vendor_wheels.sh) make the build work without PyPI; without them the
# build installs from the index.
COPY vendor/wheels/ /tmp/wheels/
RUN if ls /tmp/wheels/*.whl >/dev/null 2>&1; then \
        pip install --no-index --find-links /tmp/wheels -r requirements.txt -r requirements-controller.txt; \
    else \
        pip install -r requirements.txt -r requirements-controller.txt; \
    fi && rm -rf /tmp/wheels

COPY controller/ controller/
ARG CODE_VERSION=dev
ENV CTL_CODE_VERSION=${CODE_VERSION}
USER nobody
ENTRYPOINT ["python", "-m", "controller"]

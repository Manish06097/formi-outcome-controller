FROM docker:cli AS dk
FROM python:3.11-slim
RUN apt-get update >/dev/null && apt-get install -y --no-install-recommends git procps curl sudo >/dev/null && rm -rf /var/lib/apt/lists/*
COPY --from=dk /usr/local/bin/docker /usr/local/bin/docker

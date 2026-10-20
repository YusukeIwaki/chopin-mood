FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl git \
    && rm -rf /var/lib/apt/lists/*
RUN curl -fsSL https://api.meta.ai/muse-launcher.sh -o /usr/local/bin/muse \
    && chmod +x /usr/local/bin/muse && muse --version
WORKDIR /workspace
ENTRYPOINT ["muse"]

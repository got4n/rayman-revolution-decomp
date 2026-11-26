FROM debian:bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        binutils-mips-linux-gnu \
        cpp \
        gcc-mipsel-linux-gnu \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /work

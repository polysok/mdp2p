FROM python:3.13-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libgmp-dev \
        python3-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY requirements.txt .
RUN pip install --prefix=/install --no-cache-dir -r requirements.txt

FROM python:3.13-slim AS base

RUN apt-get update && apt-get install -y --no-install-recommends \
        libgmp10 \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 1000 mdp2p \
    && useradd --uid 1000 --gid mdp2p --create-home mdp2p

COPY --from=builder /install /usr/local

ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY --chown=mdp2p:mdp2p \
    bundle.py naming.py peer.py peer_zero.py pinstore.py wire.py ./

# Pre-create the data directory owned by the mdp2p user. When Docker
# creates a named volume for the first time, it copies ownership/contents
# from this mount point inside the image — so peer.key lands in a
# writable location from the start.
RUN mkdir -p /var/lib/mdp2p/peer_zero \
    && chown -R mdp2p:mdp2p /var/lib/mdp2p

EXPOSE 1707

USER mdp2p

# No ENTRYPOINT — docker-compose sets `command: python peer_zero.py ...`
# so the same image can be reused for different roles if needed.

FROM python:3.13-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        libgmp-dev \
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

WORKDIR /app

COPY --chown=mdp2p:mdp2p \
    bundle.py naming.py peer.py peer_zero.py pinstore.py wire.py ./

EXPOSE 1707

USER mdp2p

# No ENTRYPOINT — docker-compose sets `command: python peer_zero.py ...`
# so the same image can be reused for different roles if needed.

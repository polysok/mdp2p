FROM python:3.13-slim AS base

RUN groupadd --gid 1000 mdp2p \
    && useradd --uid 1000 --gid mdp2p --create-home mdp2p

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=mdp2p:mdp2p bundle.py protocol.py tracker.py pinstore.py ./

EXPOSE 1707

USER mdp2p

ENTRYPOINT ["python", "tracker.py"]
CMD ["--port", "1707"]

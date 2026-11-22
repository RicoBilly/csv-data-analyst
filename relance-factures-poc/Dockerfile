FROM ghcr.io/astral-sh/uv:0.11-debian-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates \
 && rm -rf /var/lib/apt/lists/*

ENV UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_INSTALL_DIR=/opt/python \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app
COPY relance-factures-poc/ /app/

# Secret injecte au runtime par le deploiement (secret workspace cle_api_pennylane ->
# env PENNYLANE_API_TOKEN). Jamais de valeur en dur ici.
ARG PENNYLANE_API_TOKEN=""
ENV PENNYLANE_API_TOKEN=${PENNYLANE_API_TOKEN}

RUN uv lock \
 && uv sync --frozen --no-dev \
 && useradd --create-home --shell /usr/sbin/nologin agent \
 && chown -R agent:agent /app

USER agent

CMD ["agents", "run-worker"]

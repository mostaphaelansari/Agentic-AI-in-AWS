# Built from the repository root, not from this directory, so the image can
# vendor rag/ — the shared corpus code plus the open-source retrieval engine
# that backs searchTravelGuides. See opensource/compose/docker-compose.yml.
#
#   docker build -f opensource/mcp-tools/Dockerfile .
FROM python:3.12-slim-bookworm

WORKDIR /app

COPY opensource/mcp-tools/requirements.txt .
RUN apt-get update \
    && apt-get upgrade -y \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir -r requirements.txt
RUN apt-get purge -y --allow-remove-essential perl-base || true \
    && apt-get purge -y --allow-remove-essential perl-modules-5.36 || true \
    && apt-get purge -y --allow-remove-essential libperl5.36 || true \
    && apt-get purge -y --allow-remove-essential perl || true \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

COPY opensource/mcp-tools/ ./
# rag.opensource imports rag.wikivoyage for the shared normalization contract,
# so the whole package ships rather than a subset.
COPY rag/ ./rag/

EXPOSE 8000

CMD ["python", "server.py"]

# Container image for the API.
#
# Python 3.12, not 3.14: PyTorch has no 3.14 wheels, so the transformer
# embedding backend cannot be installed there. Pinning 3.12 here means the
# container can run the same backend used for the paper's numbers, even though
# the dev machine falls back to TF-IDF.
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first, in their own layer, so a code change does not reinstall
# numpy and scikit-learn on every build.
COPY requirements.txt .

# sentence-transformers pulls in ~2GB of PyTorch. The image ships without it and
# uses the TF-IDF fallback; build with --build-arg WITH_TRANSFORMERS=1 for the
# full semantic path.
ARG WITH_TRANSFORMERS=0
RUN if [ "$WITH_TRANSFORMERS" = "1" ]; then \
        pip install -r requirements.txt ; \
    else \
        grep -v '^sentence-transformers' requirements.txt > /tmp/req.txt && \
        pip install -r /tmp/req.txt ; \
    fi

COPY src/ ./src/
COPY scripts/ ./scripts/
COPY data/sample_listings.json data/fx_rates.json ./data/
COPY web/ ./web/

# Run as a non-root user. The data directory must be writable because SQLite
# creates the database file and its journal there on first connect.
RUN useradd --create-home --uid 10001 appuser && \
    mkdir -p /app/data /app/logs && \
    chown -R appuser:appuser /app
USER appuser

ENV EMBEDDER_BACKEND=auto \
    LOG_FORMAT=json \
    API_HOST=0.0.0.0 \
    API_PORT=8000 \
    DB_PATH=/app/data/aggregator.db

EXPOSE 8000

# /health returns 200 even when degraded, which is correct: a missing price
# history or a synthetic source is a data-quality warning, not a dead process.
# An orchestrator restarting the container would not fix either.
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status==200 else 1)"

CMD ["python", "-m", "uvicorn", "src.api.app:app", \
     "--host", "0.0.0.0", "--port", "8000", "--log-config", "/dev/null"]

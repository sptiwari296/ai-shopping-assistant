# One image, two services (api + ui) — see docker-compose.yml.
FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HUB_DISABLE_SYMLINKS_WARNING=1 \
    FASTEMBED_CACHE_PATH=/models

COPY requirements.txt .
RUN pip install -r requirements.txt

# Download the embedding model at BUILD time, so the container starts without a 90-second download.
RUN python -c "from fastembed import TextEmbedding; TextEmbedding('sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2', cache_dir='/models')"

COPY app ./app
COPY ui ./ui
COPY scripts ./scripts
COPY data ./data

EXPOSE 8000 8501
CMD ["uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", "8000"]

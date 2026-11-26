# pyocr-toolkit with Tesseract included.
#   docker build -t pyocr-toolkit .
#   docker run --rm -p 8000:8000 --env-file .env -v pyocr-data:/data pyocr-toolkit
FROM python:3.12-slim

# Tesseract + a few common language packs (add more: tesseract-ocr-<lang>)
RUN apt-get update \
 && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng tesseract-ocr-deu \
      tesseract-ocr-fra tesseract-ocr-spa \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir .

ENV DATA_DIR=/data \
    PYTHONUNBUFFERED=1
VOLUME ["/data"]
EXPOSE 8000

# No authentication is built in: only publish this port where you trust the network.
CMD ["pyocr", "serve", "--host", "0.0.0.0", "--port", "8000"]

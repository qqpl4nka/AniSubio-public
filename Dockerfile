FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends p7zip-full \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

RUN useradd --create-home --uid 10001 anisubio \
    && mkdir -p /app/data \
    && chown -R anisubio:anisubio /app

USER anisubio
EXPOSE 8000
CMD ["uvicorn", "anisubio.main:app", "--host", "0.0.0.0", "--port", "8000"]

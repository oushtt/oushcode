FROM python:3.11-slim

WORKDIR /app

COPY pyproject.toml README.md /app/
COPY src /app/src

RUN apt-get update && apt-get install -y --no-install-recommends git \
  && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir -e .

EXPOSE 8000

CMD ["agent", "server", "--host", "0.0.0.0", "--port", "8000"]

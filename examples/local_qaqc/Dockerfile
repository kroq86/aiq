FROM python:3.12-slim

WORKDIR /workspace
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir ".[fastapi,mcp,ollama]" "minio>=7.2,<8"

COPY examples/local_qaqc /lab
WORKDIR /lab

FROM python:3.12-slim

WORKDIR /workspace
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir ".[fastapi,ollama]" "mcp>=1.27,<2" "minio>=7.2,<8"

COPY examples/local_qaqc /lab
WORKDIR /lab

FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir '.[mcp]' && useradd --uid 10001 --create-home knowseam
USER 10001:10001
ENTRYPOINT ["python", "-m", "knowseam.cli"]

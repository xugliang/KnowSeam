FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md LICENSE NOTICE.md ./
COPY src ./src
ARG KNOWSEAM_EXTRAS=mcp
RUN pip install --no-cache-dir ".[${KNOWSEAM_EXTRAS}]" && useradd --uid 10001 --create-home knowseam
USER 10001:10001
ENTRYPOINT ["python", "-m", "knowseam.cli"]

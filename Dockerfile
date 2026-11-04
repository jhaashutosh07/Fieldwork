FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONIOENCODING=utf-8 FIELDWORK_HOME=/app \
    FIELDWORK_LLM=replay FIELDWORK_PUBLIC=1 PORT=8000
COPY pyproject.toml README.md ./
COPY fieldwork ./fieldwork
RUN pip install --no-cache-dir --upgrade pip && pip install --no-cache-dir ".[postgres,otel]"
COPY data ./data
COPY evals ./evals
RUN useradd -m app && chown -R app /app
USER app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s CMD python -c "import urllib.request,os; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/healthz')"
CMD ["sh", "-c", "fieldwork serve --bootstrap --host 0.0.0.0 --port ${PORT}"]

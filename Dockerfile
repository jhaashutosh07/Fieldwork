FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY fieldwork ./fieldwork
RUN pip install --no-cache-dir ".[postgres]"
COPY data ./data
COPY evals ./evals
ENV FIELDWORK_LLM=replay
EXPOSE 8000
CMD ["fieldwork", "serve", "--host", "0.0.0.0", "--port", "8000"]

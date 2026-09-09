FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl git \
 && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
RUN pip install --upgrade pip && pip install -e ".[llm,dev]" 2>/dev/null || pip install -e ".[dev]"

COPY . .
RUN pip install -e . --no-deps

# Non-root: the API never needs write access to its own source.
RUN useradd --create-home --uid 10001 qagent && chown -R qagent:qagent /app
USER qagent

EXPOSE 8000
CMD ["uvicorn", "qagent.main:app", "--host", "0.0.0.0", "--port", "8000"]

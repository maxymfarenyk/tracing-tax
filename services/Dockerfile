# Shared image for all Python services. Build context: ./services
# Select the service with the SERVICE build arg (e.g. gateway, orders).
FROM python:3.14.10-slim-trixie

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_NO_CACHE=1

WORKDIR /app

# Install the locked environment into /app/.venv. uv is mounted only for this
# step, so it is not part of the runtime image. --locked fails the build if
# uv.lock is out of date with pyproject.toml. The adaptive sampler (scenario E)
# is a local package installed into the venv, so the SDK finds its
# `opentelemetry_traces_sampler` entry point.
COPY pyproject.toml uv.lock .python-version ./
COPY adaptive_sampler/ ./adaptive_sampler/
RUN --mount=from=ghcr.io/astral-sh/uv:0.12.23,source=/uv,target=/bin/uv \
    uv sync --locked --no-dev --no-install-project

ENV PATH="/app/.venv/bin:${PATH}"

ARG SERVICE
COPY common/ ./
COPY ${SERVICE}/ ./

EXPOSE 8000

# `opentelemetry-instrument` wraps uvicorn and applies zero-code
# instrumentation. Single worker and no --reload: both break the
# instrumentation (forked workers / reloader process).
# Whether anything is traced and sampled is controlled purely by OTEL_* env vars.
CMD ["opentelemetry-instrument", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]

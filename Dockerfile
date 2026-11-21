FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/venv
ENV PATH=/opt/venv/bin:$PATH WHISTLE_DATA_DIR=/data PORT=8000

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
ARG GIT_SHA=dev
ENV WHISTLE_VERSION=$GIT_SHA
RUN uv sync --frozen --no-dev

# uid is fixed because the host's state volume is chowned to it (homelab-infra whistle/whistle.nix).
RUN useradd --uid 10001 --no-create-home whistle
USER 10001

EXPOSE 8000
CMD ["whistle-lab"]

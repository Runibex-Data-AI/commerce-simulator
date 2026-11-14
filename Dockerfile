# cpg-commerce-simulator generator image.
#   docker compose up            (see docker-compose.yml)
#   docker run --rm IMAGE --help
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install the package (dependencies first, so source edits keep the layer cache).
COPY pyproject.toml README.m[d] ./
COPY src ./src
RUN pip install . && rm -rf build src/*.egg-info

# SQL and the default config live outside the installed package.
COPY sql ./sql
COPY config ./config

RUN useradd --uid 10001 --user-group --home-dir /app --no-create-home --shell /usr/sbin/nologin cpgsim
USER cpgsim

ENV CPGSIM_SQL_DIR=/app/sql \
    CPGSIM_CONFIG=/app/config/default.yaml

ENTRYPOINT ["sim"]
CMD ["start"]

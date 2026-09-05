# Dev container for Home Assistant custom integration development
# Based on official Home Assistant developer guide for Docker environments:
# https://developers.home-assistant.io/docs/development_environment

FROM python:3.14-slim

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=0 \
    PYTEST_PLUGINS=pytest_asyncio.plugin,pytest_homeassistant_custom_component.plugins \
    PYTEST_ASYNCIO_MODE=strict \
    PYTEST_ASYNCIO_DEFAULT_FIXTURE_LOOP_SCOPE=function

# Install system dependencies recommended by the HA developer docs for Linux
# (build-essential, libffi-dev, libssl-dev, libxml2-dev, libxslt1-dev, zlib1g-dev, etc.)
RUN apt-get update && apt-get install -y --no-install-recommends \
    bash \
    build-essential \
    autoconf \
    git \
    libffi-dev \
    libssl-dev \
    libxml2-dev \
    libxslt1-dev \
    libjpeg62-turbo-dev \
    libudev-dev \
    pkg-config \
    zlib1g-dev \
    ffmpeg \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace

ARG REQUIREMENTS_FILE=requirements-dev.txt

COPY devtools/docker/requirements*.txt devtools/docker/constraints*.txt /tmp/enphase-ev-requirements/

RUN python -m pip install --upgrade "pip==26.2.1" && \
    pip install --no-cache-dir \
        -r "/tmp/enphase-ev-requirements/${REQUIREMENTS_FILE}"

# Allow git operations (e.g. pre-commit) inside the mounted workspace.
RUN git config --global --add safe.directory /workspace

CMD ["bash"]

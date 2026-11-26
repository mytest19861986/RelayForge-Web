FROM python:3.12-slim AS wheels
WORKDIR /build
COPY pyproject.toml ./
COPY src ./src
RUN pip wheel --no-cache-dir --wheel-dir /wheels . gunicorn==23.0.0

FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 RF_CONTRACTS_DIR=/opt/relayforge/RelayForge_Architecture/contracts
WORKDIR /opt/relayforge
COPY --from=wheels /wheels /wheels
RUN pip install --no-cache-dir --no-index /wheels/*.whl && rm -rf /wheels \
    && groupadd --gid 10000 relayforge && useradd --uid 10000 --gid 10000 relayforge
COPY RelayForge_Architecture/contracts ./RelayForge_Architecture/contracts
COPY migrations ./migrations
USER 10000:10000
CMD ["gunicorn","--bind","0.0.0.0:8000","--workers","2","--threads","4","relayforge.web.wsgi:application"]

FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TADO_DATA_DIR=/data

# Usuario sin privilegios: el script no necesita ser root.
RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin tado \
    && mkdir /data \
    && chown tado:tado /data

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY tado_aa.py .

USER tado
VOLUME /data

# El script toca /data/heartbeat cada 30 s; si deja de hacerlo, el contenedor
# pasa a "unhealthy". El margen inicial cubre la primera autorización.
HEALTHCHECK --interval=1m --timeout=10s --start-period=10m --retries=3 \
    CMD ["python", "/app/tado_aa.py", "--healthcheck"]

CMD ["python", "/app/tado_aa.py"]

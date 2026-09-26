# laya-research :: realtime mutation simulator
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first so the layer caches across source edits. Only
# psycopg[binary] is needed; numpy is a generator-only dependency.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY sim ./sim

# Non-root runtime user.
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin laya \
    && chown -R laya:laya /app
USER laya

CMD ["python", "-m", "sim"]

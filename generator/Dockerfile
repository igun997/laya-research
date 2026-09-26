FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY laya_gen ./laya_gen

RUN useradd --create-home --shell /usr/sbin/nologin laya \
    && chown -R laya:laya /app
USER laya

CMD ["python", "-m", "laya_gen"]

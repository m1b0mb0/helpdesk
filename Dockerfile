FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

RUN useradd --create-home --uid 10001 app \
    && mkdir /app/staticfiles \
    && chown -R app:app /app

COPY --chown=app:app manage.py ./
COPY --chown=app:app config ./config
COPY --chown=app:app support ./support
COPY --chown=app:app docker ./docker

USER app

EXPOSE 8000

CMD ["sh", "/app/docker/start-web.sh"]

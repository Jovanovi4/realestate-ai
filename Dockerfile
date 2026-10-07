FROM python:3.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# PDF presentations require a Unicode font, including Cyrillic glyphs.
RUN apt-get update \
    && apt-get install --no-install-recommends -y fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN python -m pip install --no-cache-dir -r requirements.txt

RUN useradd --create-home --uid 10001 appuser
COPY --chown=appuser:appuser . /app

# Static assets are part of the image. Database migrations run separately at deploy time.
RUN DJANGO_DEBUG=False DJANGO_SECRET_KEY=build-only-static-key \
    python manage.py collectstatic --noinput \
    && mkdir -p /app/media \
    && chown appuser:appuser /app/media

USER appuser
EXPOSE 8000

CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "2", "--threads", "2", "--timeout", "150", "--access-logfile", "-", "--error-logfile", "-"]

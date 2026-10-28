ARG PYTHON_VERSION=3.13
FROM python:${PYTHON_VERSION}-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml ./
COPY pulsezone ./pulsezone
RUN pip install --no-cache-dir . && useradd --uid 10001 --create-home pulsezone \
    && mkdir -p /app/data /zones && chown pulsezone:pulsezone /app/data
USER pulsezone
ENV ZONE_PATH=/zones DATABASE_PATH=/app/data/pulsezone.sqlite3 WEB_HOST=0.0.0.0
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.getenv('WEB_PORT','8080')+'/healthz', timeout=3)"
CMD ["python", "-m", "pulsezone"]

FROM python:3.11.9-slim-bookworm@sha256:8fb099199b9f2d70342674bd9dbccd3ed03a258f26bbd1d556822c6dfc60c317

ARG GIT_SHA=unknown
LABEL org.opencontainers.image.revision=$GIT_SHA

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000

WORKDIR /app

RUN useradd --create-home --shell /usr/sbin/nologin appuser

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN chown -R appuser:appuser /app

USER appuser
EXPOSE 8000

# Compose supplies the worker command explicitly. Migrations are never run here.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]

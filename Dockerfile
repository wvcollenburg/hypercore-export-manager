FROM python:3.12-alpine

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./
COPY templates/ templates/

ENV HCEM_DB=/data/hcem.db \
    HCEM_PORT=8080

VOLUME /data
EXPOSE 8080

# Single worker on purpose: APScheduler lives inside the app process,
# multiple workers would mean duplicate export jobs.
CMD ["gunicorn", "--workers", "1", "--threads", "8", \
     "--bind", "0.0.0.0:8080", "--timeout", "120", "app:app"]

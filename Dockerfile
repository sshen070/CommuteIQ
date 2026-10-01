# CommuteIQ collector: a frozen snapshot of backend/app that keeps sampling
# while the code on disk changes. Rebuild to pick up new code.
FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install --no-cache-dir -r /app/backend/requirements.txt

# Same layout as the repo, so database.py's default path resolves to /app/data
WORKDIR /app/backend/app
COPY backend/app/ .

CMD ["python", "main.py"]

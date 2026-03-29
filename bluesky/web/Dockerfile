FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY bluesky/web/api/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy the full project so bluesky.* imports resolve
COPY . /app

ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

EXPOSE 8080

CMD ["uvicorn", "bluesky.web.api.main:app", "--host", "0.0.0.0", "--port", "8080"]

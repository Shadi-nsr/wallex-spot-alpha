FROM python:3.11-slim

WORKDIR /app
COPY . .

ENV PYTHONUNBUFFERED=1
EXPOSE 8080

CMD ["python3", "-m", "app.server", "--host", "0.0.0.0", "--port", "8080"]

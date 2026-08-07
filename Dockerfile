FROM python:3.11-slim

WORKDIR /app

# No pip dependencies -- stdlib only

COPY hub.py /app/

# SQLite DB is mounted as a volume at runtime
RUN mkdir -p /data

EXPOSE 8921

CMD ["python", "hub.py", "serve", "8921"]

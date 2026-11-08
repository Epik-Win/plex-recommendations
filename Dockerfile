FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir \
    requests==2.31.0 \
    plexapi

COPY plex_recommendations.py /app/

RUN mkdir -p /app/data

CMD ["python", "/app/plex_recommendations.py"]

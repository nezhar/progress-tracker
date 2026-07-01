FROM python:3.12-slim

RUN pip install --no-cache-dir "fastapi>=0.110" "uvicorn[standard]>=0.29"

RUN useradd --create-home app && mkdir /data && chown app:app /data
USER app

WORKDIR /app
COPY app.py .
COPY static ./static

ENV DATA_FILE=/data/progress.json
EXPOSE 8000
VOLUME /data

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]

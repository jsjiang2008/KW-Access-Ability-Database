FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --create-home --uid 10001 appuser \
    && mkdir /data && chown appuser:appuser /data
COPY --chown=appuser:appuser . .
USER appuser
ENV BALL_DROP_DATA_DIR=/data PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["python", "server.py", "--host", "0.0.0.0", "--behind-proxy"]

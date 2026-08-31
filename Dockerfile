FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/data
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY proxy.py ./
COPY static ./static
RUN mkdir -p /data && chown -R 10001:10001 /app /data
USER 10001
EXPOSE 11515
VOLUME ["/data"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:11515/health', timeout=3)"
CMD ["uvicorn", "proxy:app", "--host", "0.0.0.0", "--port", "11515"]

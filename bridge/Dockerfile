ARG BASE=python:3.12-slim
FROM ${BASE}
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY stoeckie_bridge.py config.example.json ./
ENV STBRIDGE_DATA=/data PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8787
HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8787/stbridge/info',timeout=4)"
CMD ["python", "stoeckie_bridge.py"]

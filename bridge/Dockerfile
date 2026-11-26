ARG BASE=python:3.14-slim
FROM ${BASE}
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --require-hashes -r requirements.txt
COPY stoeckie_bridge.py config.example.json run.sh ./
ENV STBRIDGE_DATA=/data PYTHONUNBUFFERED=1 STBRIDGE_IN_DOCKER=1
VOLUME /data
EXPOSE 8787 8443
HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8787/stbridge/info',timeout=4)"
# run.sh: restarts after a self update (exit code 75) and puts the previous version back if an update fails
CMD ["sh", "/app/run.sh"]

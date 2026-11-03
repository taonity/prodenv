FROM postgres:16-alpine

RUN apk add --no-cache python3 restic
COPY backup/retained /retained-worker

ENTRYPOINT ["python3", "/retained-worker/worker.py"]

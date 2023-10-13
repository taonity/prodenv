#!/bin/sh

/usr/bin/mc alias set minio http://minio:9000 "$MINIO_ACCESS_KEY" "$MINIO_ROOT_PASSWORD"

tail -f /dev/null
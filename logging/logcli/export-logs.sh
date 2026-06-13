docker compose run --rm logcli query '{container="fullstack-starter-backend-stage-backend-1"} |= ``' \
  --since=2m \
  --limit=500000 \
  --batch=5000 \
  --forward \
  --output=jsonl > exported_logs.txt
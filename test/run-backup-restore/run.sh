#!/bin/bash

set -e

cd "$(dirname $0)"

. ../util.sh


# docker compose up -d --quiet-pull

# webhook_logs=$(docker compose logs webhook)
# if [[ $webhook_logs != *"serving hooks on http://0.0.0.0:9000/hooks/{id}"* ]]; then
#   fail "Failed to start docker-webhook."
# fi

# responce=$(curl -X POST \
#   -H "Content-Type: application/json" \
#   -d '{
#       "repository": {
#           "name": "webhook-test-image"
#       }
#   }' \
#   http://localhost:9000/hooks/mysecret/docker-webhook)

# if [[ $responce != "A payload recieved" ]]; then
#   fail "Wrong response on POST request."
# fi

# sleep 10

# webhook_logs=$(docker compose logs webhook)
# if [[ $webhook_logs != *"Container webhook-test-image-alpine-1  Started"* ]]; then
#   fail "Failed to start webhook-test-image-alpine."
# fi

# if [[ $webhook_logs != *"finished handling mysecret/docker-webhook"* ]]; then
#   fail "Failed to finish handling the POST request."
# fi

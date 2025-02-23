#!/bin/sh

# Set the dashboard URL and download path
DASHBOARD_URL="https://grafana.com/api/dashboards/1860/revisions/latest/download"
DASHBOARD_FILE="/home/curl_user/dashboards/dashboard-1860.json"

# Download the dashboard if it doesn't exist
if [ ! -f "$DASHBOARD_FILE" ]; then
  echo "Downloading dashboard..."
  curl -o "$DASHBOARD_FILE" "$DASHBOARD_URL"
  echo "Done"
else
  echo "Dashboard already exists. Skipping download."
fi

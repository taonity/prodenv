#!/bin/sh

set -e

. ./util.sh

image_name=$(basename "$(dirname "$(pwd)")")



for dir in $(find -mindepth 1 -maxdepth 1 -type d | sort); do
  dir=$(echo "$dir" | cut -c 3-)
  echo "################################################"
  echo "Now running ${dir}"
  echo "################################################"
  echo ""

  cp -r ../backup "${dir}/backup"
  cp -r ../cicd "${dir}/cicd"
  cp -r ../logging "${dir}/logging"
  cp -r ../portainer "${dir}/portainer"
  cp -r ../docker-compose.yml "${dir}/docker-compose.yml"

  cp -r envs/* "${dir}/"

  test="${dir}/run.sh"
  eval "$test"

  remove_all_docker_containers
  
  echo ""
  echo "$test passed"
  echo ""
done

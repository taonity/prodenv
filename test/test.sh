#!/bin/bash

set -e

. ./util.sh

items=("backup" "cicd" "logging" "portainer" "docker-compose.yml")

for dir in $(find -mindepth 1 -maxdepth 1 -type d | sort); do
  dir=$(echo "$dir" | cut -c 3-)
  echo "################################################"
  echo "Now running ${dir}"
  echo "################################################"
  echo ""

  for item in "${items[@]}"; do
    cp -r "../$item" "${dir}/$item"
  done
  cp -r "${dir}/envs/"* "${dir}"

  test="${dir}/run.sh"
  eval "$test"

  remove_docker_compose_project "${dir}/docker-compose.yml"

  for item in "${items[@]}"; do
    rm -r "${dir:?}/$item"
  done
  
  echo ""
  echo "$test passed"
  echo ""
done

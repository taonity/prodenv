#!/bin/bash

set -e

docker() {
  case $1 in
    compose)
      shift
      case $1 in
        up)
          shift
          command docker compose up --timeout 3 "$@";;
        down)
          shift
          command docker compose down --timeout 3 "$@";;
        *)
          command docker compose "$@";;
      esac
      ;;
    *)
      command docker "$@";;
  esac
}

remove_all_docker_containers() {
    docker rm -f $(docker ps -aq) 2>/dev/null || echo "No containers to remove."
}

info () {
  echo "[test:${current_test:-none}:info] "$1""
}

pass () {
  echo "[test:${current_test:-none}:pass] "$1""
}

fail () {
  echo "[test:${current_test:-none}:fail] "$1""
  remove_all_docker_containers
  exit 1
}

skip () {
  echo "[test:${current_test:-none}:skip] "$1""
  remove_all_docker_containers
  exit 0
}

expect_running_containers () {
  if [ "$(docker ps -q | wc -l)" != "$1" ]; then
    fail "Expected $1 containers to be running, instead seen: "$(docker ps -a | wc -l)""
  fi
  pass "$1 containers running."
}


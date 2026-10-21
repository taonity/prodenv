#!/bin/sh
set -eu
directory="${1:?Pass the encrypted recovery export directory}"
[ -d "$directory" ]
if [ -z "$(find "$directory" -type f -name '*.age' -mmin -60 -print -quit)" ]; then
  echo 'No recent encrypted credential export is available.' >&2
  exit 1
fi
if [ -n "$(find "$directory" -type l -print -quit)" ] || [ -n "$(find "$directory" -type f ! -name '*.age' ! -name '.export-*' -print -quit)" ]; then
  echo 'Recovery backup contains an unexpected file or symbolic link.' >&2
  exit 1
fi
find "$directory" -type f -name '*.age' -exec sh -c '
  for filename do
    IFS= read -r header < "$filename"
    if [ "$header" != "age-encryption.org/v1" ]; then
      echo "Recovery file does not have an age encryption header." >&2
      exit 1
    fi
  done
' sh {} +
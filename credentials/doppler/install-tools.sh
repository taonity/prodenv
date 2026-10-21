#!/bin/sh
set -eu
prefix="${1:-/usr/local/bin}"
case "$(uname -m)" in
  x86_64)
    architecture=amd64
    doppler_sha=92587cb89c2ff82fc2ddec2b2a9f275ff320994a44e16cd9310b55a429a82084
    age_sha=cbe24006683f8eb669266162894b9a522a1af52f2665fbc63a4bb032ed26ac10
    ;;
  aarch64|arm64)
    architecture=arm64
    doppler_sha=3aa603d60bc8502ec67d9a57e917925f357ac2822eead484acda3875fe6a4d95
    age_sha=6b8dc4333c53a5a57c9e5834e3a48f92605d7154014cd07269ff3327db5d37f4
    ;;
  *) echo 'Only Linux amd64 and arm64 are supported.' >&2; exit 1 ;;
esac
[ "$(uname -s)" = Linux ]
temporary="$(mktemp -d)"
trap 'rm -rf "$temporary"' EXIT
curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 \
  "https://github.com/DopplerHQ/cli/releases/download/3.77.0/doppler_3.77.0_linux_${architecture}.tar.gz" -o "$temporary/doppler.tar.gz"
printf '%s  %s\n' "$doppler_sha" "$temporary/doppler.tar.gz" | sha256sum --check --status
curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 \
  "https://github.com/FiloSottile/age/releases/download/v1.3.2/age-v1.3.2-linux-${architecture}.tar.gz" -o "$temporary/age.tar.gz"
printf '%s  %s\n' "$age_sha" "$temporary/age.tar.gz" | sha256sum --check --status
tar -xzf "$temporary/doppler.tar.gz" -C "$temporary" doppler
tar -xzf "$temporary/age.tar.gz" -C "$temporary" age/age age/age-keygen
mkdir -p "$prefix"
install -m 755 "$temporary/doppler" "$prefix/doppler"
install -m 755 "$temporary/age/age" "$prefix/age"
install -m 755 "$temporary/age/age-keygen" "$prefix/age-keygen"
"$prefix/doppler" --version
"$prefix/age" --version
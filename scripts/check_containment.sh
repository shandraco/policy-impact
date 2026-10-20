#!/usr/bin/env bash
# Proves that no running container can reach the internet, and that the check itself works.
# Each "blocked" test is paired with a positive control that must succeed, so a broken probe
# (no DNS, no network at all) can't make the test pass for the wrong reason.
#
# Usage: scripts/check_containment.sh                      (stack up, .env in the repo root)
#        COMPOSE_ARGS="--env-file /path/to/env" scripts/check_containment.sh
# Exit code 0 = contained. Anything else = do not put real policies in.
set -u
cd "$(dirname "$0")/.."

OUTSIDE_URL="${OUTSIDE_URL:-https://example.com}"
OUTSIDE_IP="${OUTSIDE_IP:-1.1.1.1}"
# shellcheck disable=SC2206
DC=(docker compose ${COMPOSE_ARGS:-})
fail=0
checked=0

if ! services=$("${DC[@]}" ps --status running --services 2>&1); then
  echo "FAIL  could not list running services: $services"
  exit 1
fi

probe() {  # probe <service> <host> <port>  -> exit 0 if a TCP connection opens
  "${DC[@]}" exec -T "$1" python3 -c "
import socket, sys
try:
    socket.create_connection(('$2', $3), timeout=5).close()
except OSError:
    sys.exit(1)
" >/dev/null 2>&1
}

probe_url() {  # probe_url <service> <url>  -> exit 0 if the URL answers
  "${DC[@]}" exec -T "$1" python3 -c "
import sys, urllib.request
try:
    urllib.request.urlopen('$2', timeout=5)
except Exception:
    sys.exit(1)
" >/dev/null 2>&1
}

running() { grep -qx "$1" <<<"$services"; }

check() {  # check <service> <control-host> <control-port>
  local svc=$1
  if ! running "$svc"; then echo "SKIP  $svc is not running"; return; fi
  checked=$((checked + 1))
  if probe "$svc" "$2" "$3"; then
    echo "OK    $svc can reach $2:$3 (positive control)"
  else
    echo "FAIL  $svc cannot reach $2:$3, so the probe itself is broken; result below is not trustworthy"; fail=1
  fi
  if probe_url "$svc" "$OUTSIDE_URL" || probe "$svc" "$OUTSIDE_IP" 443; then
    echo "FAIL  $svc can reach the internet"; fail=1
  else
    echo "OK    $svc cannot reach the internet"
  fi
}

check policy-api db 5432
check worker db 5432
check hermes policy-api 8000
# open-webui is the one exception: it may reach the sign-in provider only (scripts/firewall.sh), so it isn't checked here.

if [ "$checked" -eq 0 ]; then
  echo "FAIL  no services were checked; start the stack first (a check that tested nothing is not a pass)"
  exit 1
fi
if [ "$fail" -eq 0 ]; then echo "PASS  contained"; else echo "FAIL  NOT contained. Fix this before loading real policies."; fi
exit "$fail"

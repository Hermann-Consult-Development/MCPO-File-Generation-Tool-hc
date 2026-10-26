#!/usr/bin/env bash
#
# Start a freshly built image and check that it serves, before any tag is
# pushed. A build can succeed while the server cannot start (an unpinned
# dependency moved): the image would still reach `latest`.
#
#   .github/scripts/probe-image.sh <file-export-server|mcpo|sse_http> <image>
#
set -euo pipefail

kind="${1:?usage: probe-image.sh <file-export-server|mcpo|sse_http> <image>}"
image="${2:?usage: probe-image.sh <kind> <image>}"
name="probe-${kind//_/-}-$$"

case "$kind" in
  file-export-server) port=9003; env_args=(-e FILE_EXPORT_DIR=/tmp/export) ;;
  mcpo)               port=8000; env_args=(-e FILE_EXPORT_DIR=/tmp/export) ;;
  sse_http)           port=9004; env_args=(-e MODE=http -e FILE_EXPORT_DIR=/tmp/export) ;;
  *) echo "unknown image kind: $kind" >&2; exit 2 ;;
esac

cleanup() { docker rm -f "$name" >/dev/null 2>&1 || true; }
trap cleanup EXIT

fail() {
  echo "::error::$kind: $1"
  docker logs --tail 80 "$name" 2>&1 || true
  exit 1
}

docker run -d --name "$name" "${env_args[@]}" -p "127.0.0.1::$port" "$image" >/dev/null
host_port=$(docker port "$name" "$port/tcp" | head -1 | sed 's/.*://')
base="http://127.0.0.1:$host_port"

# What counts as "serves" per image: the file server answers its own route,
# MCPO lists the file tools (its MCP server process started), the SSE server
# completes an MCP initialize over streamable HTTP.
# The whole answer is read before it is searched: piped into `grep -q`, a
# longer answer (MCPO's openapi.json) can make curl fail writing once grep has
# matched, and with pipefail that counts as no answer.
probe() {
  local body
  case "$kind" in
    file-export-server)
      # legacy mode: "File not found"; REQUIRE_USER_AUTH=true: the pointer to
      # the authenticated Open WebUI link.
      body=$(curl -s --max-time 5 "$base/files/probe/missing.txt") || return 1
      grep -qE 'File not found|authenticated Open WebUI file link' <<<"$body"
      ;;
    mcpo)
      body=$(curl -sf --max-time 5 "$base/file_export/openapi.json") || return 1
      grep -q '/create_file' <<<"$body"
      ;;
    sse_http)
      body=$(curl -s --max-time 5 -X POST "$base/mcp" \
        -H 'Content-Type: application/json' \
        -H 'Accept: application/json, text/event-stream' \
        -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"ci-probe","version":"0"}}}') \
        || return 1
      grep -q '"serverInfo"' <<<"$body"
      ;;
  esac
}

for _ in $(seq 1 60); do
  [ "$(docker inspect -f '{{.State.Running}}' "$name" 2>/dev/null)" = true ] \
    || fail "container exited during startup"
  if probe; then
    # Still up a moment later: no crash right after the first answer.
    sleep 3
    [ "$(docker inspect -f '{{.State.Running}}' "$name")" = true ] \
      || fail "container exited after its first answer"
    echo "$kind: image starts and serves ($image)"
    exit 0
  fi
  sleep 2
done
fail "no answer within 120 s"

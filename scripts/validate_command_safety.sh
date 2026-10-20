#!/usr/bin/env bash
PAYLOAD="$(cat)"
if echo "${PAYLOAD}" | grep -Eq 'rm -rf /|DROP DATABASE|ALPACA_PAPER_TRADE=false'; then
  echo '{"decision": "deny", "reason": "Blocked destructive command or unauthorized live-trading switch."}'
  exit 0
fi
echo '{"decision": "allow"}'

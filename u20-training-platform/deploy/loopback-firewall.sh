#!/bin/bash
set -euo pipefail
chain=PRO6000D_LOCAL
if [ "${1:-start}" = stop ]; then
  iptables -D INPUT -p tcp --dport 16817:16818 -j "$chain" 2>/dev/null || true
  iptables -F "$chain" 2>/dev/null || true
  iptables -X "$chain" 2>/dev/null || true
  ip6tables -D INPUT -p tcp --dport 16817:16818 -j "$chain" 2>/dev/null || true
  ip6tables -F "$chain" 2>/dev/null || true
  ip6tables -X "$chain" 2>/dev/null || true
  exit 0
fi
for command in iptables ip6tables; do
  "$command" -N "$chain" 2>/dev/null || true
  "$command" -F "$chain"
  "$command" -A "$chain" -i lo -j ACCEPT
  "$command" -A "$chain" -j REJECT
  "$command" -C INPUT -p tcp --dport 16817:16818 -j "$chain" 2>/dev/null || "$command" -I INPUT 1 -p tcp --dport 16817:16818 -j "$chain"
done

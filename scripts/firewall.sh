#!/usr/bin/env bash
# Linux host only. Lets Open WebUI's sign-in network reach your sign-in provider on 443 and nothing else.
# Usage: sudo scripts/firewall.sh login.microsoftonline.com graph.microsoft.com
# Re-run after the provider's addresses change. Docker Desktop on a Mac ignores DOCKER-USER rules.
set -euo pipefail
NET=172.31.250.0/24
[ $# -gt 0 ] || { echo "Name your sign-in provider's hostnames"; exit 1; }
iptables -N PI_SIGNIN 2>/dev/null || iptables -F PI_SIGNIN
iptables -C DOCKER-USER -s "$NET" -j PI_SIGNIN 2>/dev/null || iptables -I DOCKER-USER -s "$NET" -j PI_SIGNIN
iptables -A PI_SIGNIN -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN
iptables -A PI_SIGNIN -d 172.16.0.0/12 -j RETURN
iptables -A PI_SIGNIN -p udp --dport 53 -j RETURN
for host in "$@"; do
  for ip in $(getent ahostsv4 "$host" | awk '{print $1}' | sort -u); do
    iptables -A PI_SIGNIN -d "$ip" -p tcp --dport 443 -j RETURN
    echo "allow $host $ip"
  done
done
iptables -A PI_SIGNIN -j DROP
echo "Open WebUI can now reach only: $*"

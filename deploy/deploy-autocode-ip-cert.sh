#!/bin/sh
set -eu

LINEAGE="${RENEWED_LINEAGE:-/etc/letsencrypt/live/34.142.199.209}"
TLS_DIR="/home/dev/corecoder-web/tls"

install -o dev -g dev -m 0644 \
  "${LINEAGE}/fullchain.pem" \
  "${TLS_DIR}/letsencrypt-fullchain.pem"
install -o dev -g dev -m 0600 \
  "${LINEAGE}/privkey.pem" \
  "${TLS_DIR}/letsencrypt-privkey.pem"

if systemctl is-active --quiet corecoder-web.service; then
  systemctl restart corecoder-web.service
fi

#!/bin/sh
set -eu
: "${RF_ADMIN_CIDR:?Set the authorized SSH administration CIDR}"
ufw default deny incoming
ufw default allow outgoing
ufw allow from "$RF_ADMIN_CIDR" to any port 22 proto tcp
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable

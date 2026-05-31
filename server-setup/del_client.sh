#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────
# del_client.sh — Remove an AmneziaWG client peer
# Usage: ./del_client.sh <client_name>
# ─────────────────────────────────────────────────────────
set -euo pipefail

CLIENT_NAME="${1:?Usage: $0 <client_name>}"
CONFIG_DIR="/opt/amnezia/configs"
WG_CONF="/etc/wireguard/wg0.conf"

# Find and get the client's public key from saved config
CLIENT_CONF="${CONFIG_DIR}/${CLIENT_NAME}.conf"
if [ ! -f "$CLIENT_CONF" ]; then
    echo "Error: Client config not found: ${CLIENT_CONF}" >&2
    exit 1
fi

# Extract private key and derive public key
CLIENT_PRIVATE=$(grep "PrivateKey" "$CLIENT_CONF" | awk '{print $3}')
CLIENT_PUBLIC=$(echo "$CLIENT_PRIVATE" | wg pubkey)

# Remove peer from running interface
wg set wg0 peer "$CLIENT_PUBLIC" remove 2>/dev/null || true

# Remove peer block from config file
# Find the line with the client name comment and delete until next [Peer] or EOF
python3 -c "
import re
with open('$WG_CONF', 'r') as f:
    content = f.read()

# Remove the peer block for this client
pattern = r'\n# ${CLIENT_NAME}\n\[Peer\].*?(?=\n# |\n\[Interface\]|\Z)'
content = re.sub(pattern, '', content, flags=re.DOTALL)

with open('$WG_CONF', 'w') as f:
    f.write(content.rstrip() + '\n')
"

# Remove saved config
rm -f "$CLIENT_CONF"

echo "Client ${CLIENT_NAME} removed successfully"

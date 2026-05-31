#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────
# gen_client.sh — Generate a new AmneziaWG client config
# Usage: ./gen_client.sh <client_name>
# Returns: vpn:// encoded config string
# ─────────────────────────────────────────────────────────
set -euo pipefail

CLIENT_NAME="${1:?Usage: $0 <client_name>}"
CONFIG_DIR="/opt/amnezia/configs"
PARAMS_FILE="/opt/amnezia/server_params.json"

# Check params exist
if [ ! -f "$PARAMS_FILE" ]; then
    echo "Error: Server params not found at $PARAMS_FILE" >&2
    exit 1
fi

# Read server params
SERVER_IP=$(jq -r '.server_ip' "$PARAMS_FILE")
SERVER_PORT=$(jq -r '.server_port' "$PARAMS_FILE")
SERVER_PUB=$(jq -r '.server_public_key' "$PARAMS_FILE")
JC=$(jq -r '.jc' "$PARAMS_FILE")
JMIN=$(jq -r '.jmin' "$PARAMS_FILE")
JMAX=$(jq -r '.jmax' "$PARAMS_FILE")
S1=$(jq -r '.s1' "$PARAMS_FILE")
S2=$(jq -r '.s2' "$PARAMS_FILE")
H1=$(jq -r '.h1' "$PARAMS_FILE")
H2=$(jq -r '.h2' "$PARAMS_FILE")
H3=$(jq -r '.h3' "$PARAMS_FILE")
H4=$(jq -r '.h4' "$PARAMS_FILE")

# Generate client keys
CLIENT_PRIVATE=$(wg genkey)
CLIENT_PUBLIC=$(echo "$CLIENT_PRIVATE" | wg pubkey)
PSK=$(wg genpsk)

# Find next available IP
LAST_IP=$(grep -oP 'AllowedIPs = 10\.8\.1\.\K[0-9]+' /etc/wireguard/wg0.conf 2>/dev/null | sort -n | tail -1)
NEXT_IP=$((${LAST_IP:-1} + 1))

if [ "$NEXT_IP" -ge 255 ]; then
    echo "Error: No available IPs" >&2
    exit 1
fi

CLIENT_IP="10.8.1.${NEXT_IP}"

# Add peer to server config
cat >> /etc/wireguard/wg0.conf <<EOF

# ${CLIENT_NAME}
[Peer]
PublicKey = ${CLIENT_PUBLIC}
PresharedKey = ${PSK}
AllowedIPs = ${CLIENT_IP}/32
EOF

# Reload WireGuard
wg syncconf wg0 <(wg-quick strip wg0)

# Build client config
CLIENT_CONFIG="[Interface]
PrivateKey = ${CLIENT_PRIVATE}
Address = ${CLIENT_IP}/32
DNS = 1.1.1.1, 8.8.8.8
Jc = ${JC}
Jmin = ${JMIN}
Jmax = ${JMAX}
S1 = ${S1}
S2 = ${S2}
H1 = ${H1}
H2 = ${H2}
H3 = ${H3}
H4 = ${H4}

[Peer]
PublicKey = ${SERVER_PUB}
PresharedKey = ${PSK}
AllowedIPs = 0.0.0.0/0
Endpoint = ${SERVER_IP}:${SERVER_PORT}
PersistentKeepalive = 25"

# Save config locally
mkdir -p "$CONFIG_DIR"
echo "$CLIENT_CONFIG" > "${CONFIG_DIR}/${CLIENT_NAME}.conf"

# Encode as vpn:// URI
VPN_URL="vpn://$(echo -n "$CLIENT_CONFIG" | base64 -w0 | tr '+/' '-_' | tr -d '=')"

# Output the vpn:// key
echo "$VPN_URL"

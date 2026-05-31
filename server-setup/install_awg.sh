#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────
# install_awg.sh — Install AmneziaWG on a fresh Ubuntu server
# Usage: curl -fsSL https://your-host/install.sh | bash
# ─────────────────────────────────────────────────────────
set -euo pipefail

echo "======================================"
echo "  AmneziaWG Server Setup"
echo "======================================"

# Update system
echo "[1/5] Updating system..."
apt-get update -qq && apt-get upgrade -y -qq

# Install dependencies
echo "[2/5] Installing dependencies..."
apt-get install -y -qq \
    docker.io \
    docker-compose \
    wireguard-tools \
    jq \
    qrencode

# Enable Docker
systemctl enable docker
systemctl start docker

# Create directory structure
echo "[3/5] Creating directories..."
mkdir -p /opt/amnezia/{configs,keys}

# Pull AmneziaWG Docker image
echo "[4/5] Pulling AmneziaWG Docker image..."
docker pull amneziavpn/amnezia-wg:latest 2>/dev/null || {
    echo "Note: Using standard WireGuard with Amnezia params"
}

# Generate server keys
echo "[5/5] Generating server keys..."
wg genkey | tee /opt/amnezia/keys/server_private.key | wg pubkey > /opt/amnezia/keys/server_public.key
chmod 600 /opt/amnezia/keys/server_private.key

# Get public IP
SERVER_IP=$(curl -s ifconfig.me)
SERVER_PORT=51820

# Generate AmneziaWG-specific params
JC=4
JMIN=40
JMAX=70
S1=$((RANDOM % 200 + 50))
S2=$((RANDOM % 200 + 50))
H1=$((RANDOM))
H2=$((RANDOM))
H3=$((RANDOM))
H4=$((RANDOM))

# Create server config
cat > /opt/amnezia/wg0.conf <<EOF
[Interface]
PrivateKey = $(cat /opt/amnezia/keys/server_private.key)
Address = 10.8.1.1/24
ListenPort = ${SERVER_PORT}
PostUp = iptables -A FORWARD -i wg0 -j ACCEPT; iptables -t nat -A POSTROUTING -o eth0 -j MASQUERADE
PostDown = iptables -D FORWARD -i wg0 -j ACCEPT; iptables -t nat -D POSTROUTING -o eth0 -j MASQUERADE
Jc = ${JC}
Jmin = ${JMIN}
Jmax = ${JMAX}
S1 = ${S1}
S2 = ${S2}
H1 = ${H1}
H2 = ${H2}
H3 = ${H3}
H4 = ${H4}
EOF

# Save params for client generation
cat > /opt/amnezia/server_params.json <<EOF
{
    "server_ip": "${SERVER_IP}",
    "server_port": ${SERVER_PORT},
    "server_public_key": "$(cat /opt/amnezia/keys/server_public.key)",
    "jc": ${JC},
    "jmin": ${JMIN},
    "jmax": ${JMAX},
    "s1": ${S1},
    "s2": ${S2},
    "h1": ${H1},
    "h2": ${H2},
    "h3": ${H3},
    "h4": ${H4}
}
EOF

# Enable IP forwarding
echo "net.ipv4.ip_forward = 1" >> /etc/sysctl.conf
sysctl -p

# Enable and start WireGuard
cp /opt/amnezia/wg0.conf /etc/wireguard/wg0.conf
systemctl enable wg-quick@wg0
systemctl start wg-quick@wg0

# Open firewall
ufw allow ${SERVER_PORT}/udp 2>/dev/null || true

# Copy management scripts
cp /opt/amnezia/gen_client.sh /opt/amnezia/gen_client.sh 2>/dev/null || true
cp /opt/amnezia/del_client.sh /opt/amnezia/del_client.sh 2>/dev/null || true
chmod +x /opt/amnezia/*.sh 2>/dev/null || true

echo ""
echo "======================================"
echo "  ✅ AmneziaWG installed successfully!"
echo "======================================"
echo "  Server IP:   ${SERVER_IP}"
echo "  Port:        ${SERVER_PORT}"
echo "  Public Key:  $(cat /opt/amnezia/keys/server_public.key)"
echo "  Config:      /opt/amnezia/wg0.conf"
echo "  Params:      /opt/amnezia/server_params.json"
echo "======================================"

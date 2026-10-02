#!/usr/bin/env bash
# One-time setup on a fresh Oracle Cloud Ubuntu VM (Ampere A1 / ARM64 or x86).
# Usage:  bash deploy/oracle-setup.sh
set -euo pipefail

PORT="${FILE_SERVER_PORT:-8080}"

echo "==> Installing Docker"
if ! command -v docker >/dev/null; then
  curl -fsSL https://get.docker.com | sudo sh
  sudo usermod -aG docker "$USER"
fi

echo "==> Opening port $PORT in the VM firewall (Oracle Ubuntu images block it by default)"
if sudo iptables -C INPUT -p tcp --dport "$PORT" -j ACCEPT 2>/dev/null; then
  echo "    already open"
else
  sudo iptables -I INPUT 6 -p tcp -m state --state NEW --dport "$PORT" -j ACCEPT
  sudo apt-get install -y iptables-persistent >/dev/null 2>&1 || true
  sudo netfilter-persistent save || true
fi

echo "==> Adding 4 GB swap (helps during Docker builds on small shapes)"
if [ ! -f /swapfile ]; then
  sudo fallocate -l 4G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile
  echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
fi

if [ ! -f .env ]; then
  cp .env.example .env
  echo "==> Created .env, fill in your tokens:  nano .env"
fi

cat <<EOF

Done. Next steps:
  1. In the Oracle console: Networking > VCN > Security List > add an Ingress rule for TCP $PORT (0.0.0.0/0)
  2. nano .env       (Discord tokens, Anthropic key, PUBLIC_BASE_URL=http://<this VM's public IP>:$PORT)
  3. Log out and back in (so your user can run docker), then:
       docker compose up -d --build
       docker compose logs -f
EOF

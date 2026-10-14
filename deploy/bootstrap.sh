#!/usr/bin/env bash
# One-time server setup for Ubuntu 24.04 LTS. Run as root: sudo ./bootstrap.sh
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "Run with sudo"; exit 1; }
DEPLOY_USER="${SUDO_USER:-ubuntu}"

export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y ca-certificates curl git ufw unzip fail2ban unattended-upgrades docker.io docker-compose-v2

# Docker log rotation (a full disk is the most common way a small server dies)
mkdir -p /etc/docker
cat > /etc/docker/daemon.json <<'JSON'
{ "log-driver": "json-file", "log-opts": { "max-size": "10m", "max-file": "3" } }
JSON
systemctl enable --now docker
systemctl restart docker
usermod -aG docker "$DEPLOY_USER"

# AWS CLI (Ubuntu has no apt package for v2)
command -v aws >/dev/null 2>&1 || snap install aws-cli --classic

# 2 GB swap so a memory spike slows things down instead of killing the database
if ! swapon --show | grep -q .; then
  fallocate -l 2G /swapfile
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
  echo 'vm.swappiness=10' > /etc/sysctl.d/99-swappiness.conf
  sysctl -p /etc/sysctl.d/99-swappiness.conf
fi

# Firewall (the AWS security group should also allow only 22 from your IP, and 80/443)
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable

systemctl enable --now fail2ban
dpkg-reconfigure -f noninteractive unattended-upgrades

mkdir -p /opt/pumpwise
chown "$DEPLOY_USER":"$DEPLOY_USER" /opt/pumpwise
echo "Done. Log out and back in so the docker group applies, then continue with the README."

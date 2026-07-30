#!/bin/bash
# Hetzner "Cloud config" box — paste this whole file. Runs once, at first
# boot, as root. Output lands in /var/log/cloud-init-output.log.
#
# Prepares the OS only. The app is installed afterwards by hand, because that
# needs the code and DNS, neither of which exist yet at this point.
set -eux
export DEBIAN_FRONTEND=noninteractive

# --- packages ---------------------------------------------------------------
apt-get update
apt-get -y -o Dpkg::Options::=--force-confold upgrade
apt-get -y install \
  git curl ufw nginx certbot python3-certbot-nginx \
  python3 python3-venv sqlite3 fail2ban unattended-upgrades

# --- service account --------------------------------------------------------
# Runs the two services. No shell, no login, no password.
id -u otk >/dev/null 2>&1 || useradd --system --shell /usr/sbin/nologin \
  --home-dir /var/lib/odoo-tickets otk

install -d -o otk  -g otk  -m 750 /var/lib/odoo-tickets
install -d -o root -g otk  -m 750 /etc/odoo-tickets

# --- config -----------------------------------------------------------------
# OTK_SECRET is intentionally absent: the app generates secret.key (0600) in
# the data dir on first run, so the pepper never sits in a second place.
cat > /etc/odoo-tickets/env <<'EOF'
OTK_DATA_DIR=/var/lib/odoo-tickets
OTK_HOST=127.0.0.1
OTK_PORT=8787
OTK_WEB_PORT=8788
OTK_TZ=Europe/Lisbon
OTK_RETENTION_DAYS=0
EOF
chown root:otk /etc/odoo-tickets/env
chmod 640 /etc/odoo-tickets/env

# --- firewall ---------------------------------------------------------------
# 22/80/443 in, everything out. 8787/8788 stay on loopback and are never
# opened; nginx reaches them internally.
ufw default deny incoming
ufw default allow outgoing
ufw limit 22/tcp
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable

# --- ssh --------------------------------------------------------------------
# A drop-in numbered 99 so it wins over the image's own cloud-init config,
# which usually sets PasswordAuthentication itself.
cat > /etc/ssh/sshd_config.d/99-hardening.conf <<'EOF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
EOF
systemctl restart ssh || systemctl restart sshd

systemctl enable --now fail2ban unattended-upgrades

echo "OS ready. Next: rsync the code to /opt/odoo-tickets, then install."

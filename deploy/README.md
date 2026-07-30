# Deploying on a Hetzner box

Nothing here has been run against a real server — it is written from the app's
actual requirements, but verify each step rather than pasting blind.

## Hetzner-specific

**There are two firewalls, and they are unrelated.** The Cloud Firewall is
applied in the Hetzner console/API at the network edge, before packets reach
the machine; ufw/nftables runs on the host. Use both. The cloud one keeps
protecting you if the host rules are flushed by a bad `nft -f`; the host one
keeps protecting you if a server gets attached to the wrong firewall label.

**Adding any outbound rule to the Cloud Firewall makes outbound deny-by-default.**
With no outbound rules, all egress is permitted. Add one and everything else is
dropped — including DNS and NTP, which fails in confusing ways. If you go that
route you need 53/udp+tcp, 123/udp and 80+443/tcp before apt or certificate
renewal will work.

**Cloud servers get IPv6 by default** (a /64). Rules written only for IPv4
leave the machine reachable over v6. Use nftables' `inet` family, or confirm
`IPV6=yes` in `/etc/default/ufw`.

**Cloud Firewall is stateful; the Robot firewall for dedicated servers is not.**
If this is a dedicated box rather than Cloud, a naive inbound-only ruleset
silently drops the return traffic of your own outbound connections.

**Outbound port 25 is blocked by default.** Irrelevant today — this app sends
no mail — but it will surprise you if email notifications ever get added.

**Screenshots are the thing that grows.** If the root disk is small, attach a
Volume and point `OTK_DATA_DIR` at it. `otk usage` reports actual consumption;
`otk purge` reclaims it while keeping ticket text.

## Install

```bash
useradd --system --home /var/lib/odoo-tickets --shell /usr/sbin/nologin otk
install -d -o otk -g otk -m 750 /var/lib/odoo-tickets
install -d -o root -g otk -m 750 /etc/odoo-tickets

git clone <repo> /opt/odoo-tickets && cd /opt/odoo-tickets
uv venv && uv pip install -e .

cp deploy/otk.env.example /etc/odoo-tickets/env
chown root:otk /etc/odoo-tickets/env && chmod 640 /etc/odoo-tickets/env
$EDITOR /etc/odoo-tickets/env          # set OTK_TZ at minimum

cp deploy/otk-*.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now otk-api otk-web
systemctl status otk-api otk-web

# Confirm both are actually listening before setting up the proxy — a service
# that starts and then exits looks identical to one that never started, until
# you try to connect and get "connection refused".
curl -s localhost:8787/health
curl -sI localhost:8788/login | head -1
```

The sandboxing directives in the units are aggressive. If a service fails to
start, relax them one at a time — `SystemCallFilter` and `MemoryDenyWriteExecute`
are the usual culprits with C extensions. Check your score with:

```bash
systemd-analyze security otk-api
```

## First run

```bash
sudo -u otk /opt/odoo-tickets/.venv/bin/otk operator add jose   # web login
sudo -u otk /opt/odoo-tickets/.venv/bin/otk client add "Acme"   # prints a key ONCE
```

## Reverse proxy

Use `Caddyfile.example` (certificates handled for you) or
`nginx.conf.example`. Two rules matter regardless of which:

- **Body limit ≥ 40 MB.** nginx defaults to **1 MB**, which rejects every
  screenshot with a 413 the application never sees — so the module looks broken
  and `otk inspect` shows nothing arriving.
- **`X-Forwarded-Proto` must be set.** Without it the operator UI cannot tell
  the request arrived over TLS and never marks the session cookie `Secure`.

## Retention timer

Only if you want unattended purging. Set `OTK_RETENTION_DAYS` first — it is 0
(off) by default, and `--auto` does nothing while it is.

```ini
# /etc/systemd/system/otk-purge.service
[Service]
Type=oneshot
User=otk
EnvironmentFile=/etc/odoo-tickets/env
ExecStart=/opt/odoo-tickets/.venv/bin/otk purge --auto --yes
```
```ini
# /etc/systemd/system/otk-purge.timer
[Timer]
OnCalendar=weekly
Persistent=true
[Install]
WantedBy=timers.target
```

Run `otk purge --older-than N` by hand once first and read what it lists.
`--yes` skips the confirmation, and this deletes files permanently.

## Backups

`backup.sh` copies the database with SQLite's online backup API (a plain `cp`
of a live WAL database can restore corrupt), plus `secret.key` and the blobs.

```ini
# /etc/systemd/system/otk-backup.service
[Service]
Type=oneshot
User=root
ExecStart=/opt/odoo-tickets/deploy/backup.sh /var/backups/odoo-tickets
```

Pair with a daily timer, and **copy it off the machine** — a backup that only
exists on the server it is backing up is not one. Hetzner's own snapshots and
backups cover the disk, which is a reasonable second layer, but they restore
the whole server rather than one ticket.

Guard `secret.key` specifically: lose it and every API key you have issued
stops working, with no recovery path other than issuing new ones to every
client. Leak it and those keys become forgeable.

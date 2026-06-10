# Euro-Office DocumentServer × Nextcloud — Integration Runbook

> **🌐 PUBLIC / SANITISED VERSION**
> Hostnames, IP addressing and identifying details have been replaced with placeholders (`example.com`, RFC 1918 / documentation addresses). No secrets appear in this document. Substitute your own values throughout.

| | |
|---|---|
| **Maintainer** | Richard Carragher (RC COMMS) |
| **Last updated** | 10 June 2026 |
| **Document version** | 1.0 |
| **Proxmox VE version** | 9.1.11 (kernel 7.0.2-2-pve) |
| **Euro-Office DocumentServer** | 9.3.1 (build 37) |
| **Nextcloud** | Hub 26 Spring (34.0.0), TurnKey Linux appliance |
| **Connector app** | `eurooffice` 11.0.0 |
| **Classification** | Public |

---

## Table of contents

1. [Overview](#1-overview)
2. [Architecture](#2-architecture)
3. [Build procedure](#3-build-procedure)
4. [TLS and DNS](#4-tls-and-dns)
5. [Apache reverse proxy](#5-apache-reverse-proxy)
6. [Nextcloud connector](#6-nextcloud-connector)
7. [Known quirks and gotchas](#7-known-quirks-and-gotchas)
8. [Operational runbook](#8-operational-runbook)
9. [Troubleshooting](#9-troubleshooting)
10. [Family usage](#10-family-usage)
11. [Explain like I'm 5](#11-explain-like-im-5)
12. [References](#12-references)
13. [Appendices](#13-appendices)

---

## 1. Overview

Euro-Office DocumentServer (the community fork of ONLYOFFICE DocumentServer, adopted by Nextcloud as a first-party office suite after the ONLYOFFICE partnership ended in 2026) provides collaborative in-browser editing of DOCX/XLSX/PPTX/PDF inside Nextcloud Files. It runs in a dedicated Debian 12 LXC (CT 141) on a Proxmox VE host, fronted by the existing Apache instance on the Nextcloud VM, which acts as a name-based TLS-terminating reverse proxy.

The service is reachable inside and outside the LAN at `https://office.example.com` on the standard port 443 — no additional ports were opened. All clients (Fedora 44 workstation, Android, Surface Pro) use it through a browser or the Nextcloud apps; nothing is installed client-side.

### Why this design?

1. **Separate LXC, not co-hosted on the Nextcloud VM** — the document server is heavy (Node.js, PostgreSQL, RabbitMQ, Redis, nginx), is updated on its own cadence, and can be rebooted/rebuilt without touching Nextcloud.
2. **Apache name-based vhost rather than a new port** — the router already forwards 80/443 to the Nextcloud VM. A second vhost on the same 443, routed by SNI/Host header, avoids non-standard ports and extra firewall rules.
3. **Public DNS is mandatory** — the editor JavaScript loads in the *user's browser* directly from the document server (Nextcloud only embeds it in an iframe), so the document server URL must resolve and be reachable from wherever the user sits. Server-to-server callback traffic stays on the LAN via the connector's internal-address settings and an `/etc/hosts` entry.
4. **DNS-01 (Cloudflare) for the new certificate** — no dependency on port 80, renews unattended, and coexists with the TurnKey appliance's existing dehydrated/HTTP-01 renewal for the Nextcloud certificate.

---

## 2. Architecture

### 2.1 Components

| Component | Host | IP | Notes |
|---|---|---|---|
| Nextcloud Hub 26 (34.0.0) | `nextcloud` VM (TurnKey Linux, Debian-based) | 10.0.0.40 | Apache 2.4 terminates TLS for **both** hostnames; MySQL; Redis (unix socket); data dir `/var/www/nextcloud-data` |
| Euro-Office DocumentServer 9.3.1 | CT 141 `eurooffice` (Debian 12 LXC) | 10.0.0.41 | nginx :80 → docservice :8000; PostgreSQL; RabbitMQ; Redis |
| Router / firewall | Consumer router | 10.0.0.1 | Forwards WAN 80/443 → 10.0.0.40 only |
| DNS | Cloudflare (authoritative DNS) | — | Both A records → <WAN_IP>, **DNS-only (grey cloud)** |

### 2.2 CT 141 specification

| | |
|---|---|
| VMID / hostname | 141 / `eurooffice` |
| Template | `debian-12-standard_12.12-1_amd64.tar.zst` |
| Type | Unprivileged LXC, `nesting=1`, `onboot=1` |
| CPU / RAM / swap | 4 cores / 4.00 GiB / 512 MiB |
| Root disk | `vmstorage:subvol-141-disk-0`, 15 GB |
| Network | `eth0` on `vmbr0`, 10.0.0.41/24, gw 10.0.0.1 |
| Timezone | Europe/London |
| Services | `ds-docservice` (:8000), `ds-converter`, `ds-metrics`, `nginx` (:80), `postgresql`, `redis-server`, `rabbitmq-server`, `supervisor` |
| Disabled | `ds-example` (built-in example app — deliberately left disabled; exposure risk) |

### 2.3 Traffic flow

```
Browser (anywhere)
  └─ https://office.example.com:443
       └─ router port-forward → 10.0.0.40:443 (Apache, SNI-routed vhost)
            └─ reverse proxy (HTTP + WebSocket upgrade) → 10.0.0.41:80 (nginx)
                 └─ proxied to docservice :8000

Nextcloud → DocumentServer callbacks : http://10.0.0.41/  (LAN, connector "internal" URL)
DocumentServer → Nextcloud callbacks : https://cloud.example.com
                                        (pinned to 10.0.0.40 via /etc/hosts on CT 141)
```

All Nextcloud↔DocumentServer requests are signed with a shared JWT secret (HS256). Cache-file downloads from the document server are additionally protected by nginx `secure_link` MD5 signing.

---

## 3. Build procedure

### 3.1 Create the LXC (on pve)

```bash
pveam update
pveam download local debian-12-standard_12.12-1_amd64.tar.zst

pct create 141 local:vztmpl/debian-12-standard_12.12-1_amd64.tar.zst \
  --hostname eurooffice --memory 4096 --cores 4 \
  --rootfs vmstorage:15 --unprivileged 1 --features nesting=1 \
  --net0 name=eth0,bridge=vmbr0,ip=10.0.0.41/24,gw=10.0.0.1 \
  --onboot 1
pct start 141 && pct enter 141
```

Root has no password by default — access via `pct enter 141`, or set one with `pct exec 141 -- passwd`.

### 3.2 Install Euro-Office (inside CT 141)

```bash
apt update && apt upgrade -y
timedatectl set-timezone Europe/London

# Enable contrib (ttf-mscorefonts-installer)
sed -i 's/ main$/ main contrib/' /etc/apt/sources.list
apt update

# Prerequisites — all in stock Debian 12 repos
apt install -y postgresql redis-server rabbitmq-server nginx supervisor wget gnupg

# Database (installer connects during postinst)
su - postgres -c "psql -c \"CREATE USER ds WITH PASSWORD 'ds';\""
su - postgres -c "psql -c \"CREATE DATABASE ds OWNER ds;\""

# Pre-seed installer answers
echo "ds ds/db-type select postgres
ds ds/db-host string localhost
ds ds/db-port string 5432
ds ds/db-user string ds
ds ds/db-pwd password ds
ds ds/db-name string ds" | debconf-set-selections

# Install the amd64 .deb (NOT the Fedora RPM)
wget "https://github.com/Euro-Office/DocumentServer/releases/download/v9.3.1/euro-office-documentserver_9.3.1-0_amd64.deb" -O /tmp/eo.deb
apt install -y /tmp/eo.deb

systemctl is-active ds-docservice ds-converter ds-metrics nginx
curl http://localhost/healthcheck     # expect: true
```

### 3.3 JWT secret — **set in `default.json`** (see Quirk 1)

```bash
openssl rand -hex 32     # generate; record in password manager
```

Edit `/etc/euro-office/documentserver/default.json` → `services.CoAuthoring.token.secret` and set the **same value** in all three slots: `inbox`, `outbox`, `browser` (replacing the literal `"secret"`). Mirror the value into `/etc/euro-office/documentserver/local.json` (structure in Appendix B) so a future package fix to the config loader cannot silently change the effective secret.

```bash
systemctl restart ds-docservice ds-converter
python3 /root/jwttest.py     # verify — see Appendix C
```

### 3.4 secure_link secret — align nginx and docservice (see Quirk 2)

```bash
NEWSL=$(openssl rand -hex 16)    # record in password manager

# docservice side (live config)
sed -i "s/\"secretString\": \".*\"/\"secretString\": \"$NEWSL\"/" \
  /etc/euro-office/documentserver/default.json

# nginx side — live conf AND templates (so a re-render doesn't regress it)
sed -i "s/set \$secure_link_secret .*;/set \$secure_link_secret $NEWSL;/" \
  /etc/euro-office/documentserver/nginx/ds.conf \
  /etc/euro-office/documentserver/nginx/ds.conf.tmpl \
  /etc/euro-office/documentserver/nginx/ds-ssl.conf.tmpl

nginx -t && systemctl reload nginx
systemctl restart ds-docservice ds-converter
```

### 3.5 LAN-side callback resolution

```bash
echo "10.0.0.40 cloud.example.com" >> /etc/hosts
```

Keeps DocumentServer→Nextcloud callbacks on the LAN regardless of hairpin-NAT behaviour on the router.

---

## 4. TLS and DNS

### 4.1 DNS records (Cloudflare zone `example.com`)

| Record | Type | Value | Proxy |
|---|---|---|---|
| `nextcloud` | A | <WAN_IP> | DNS only |
| `office` | A | <WAN_IP> | DNS only |

Both stay **grey-cloud**: Cloudflare's proxied free tier imposes a 100 MB upload cap that breaks large Nextcloud syncs, and DNS-only keeps both names behaving identically. The WAN IP is residential — if it ever changes, both records must be updated (a Cloudflare-API DDNS script is a known future task).

### 4.2 Two certificates, two renewal mechanisms (deliberate)

| Hostname | Issuer/CA | Challenge | Client | Cert path | Renewal |
|---|---|---|---|---|---|
| `cloud.example.com` | Let's Encrypt (E8) | HTTP-01 | TurnKey confconsole / dehydrated | `/etc/ssl/private/cert.pem` (referenced globally in `/etc/apache2/mods-available/ssl.conf`) | `/etc/cron.daily/confconsole-dehydrated` |
| `office.example.com` | Let's Encrypt | **DNS-01** (Cloudflare) | certbot + `python3-certbot-dns-cloudflare` | `/etc/letsencrypt/live/office.example.com/` | `certbot.timer`, deploy hook reloads Apache |

The HTTP-01 mechanism requires the port-80 redirect vhost in `nextcloud.conf` to remain in place. **Both renewal mechanisms run on the Nextcloud VM** — `certbot renew --dry-run` executed on CT 141 reports "No simulated renewals were attempted" because no certificates live there.

### 4.3 certbot DNS-01 setup (Nextcloud VM)

Cloudflare API token: dashboard → My Profile → API Tokens → "Edit zone DNS" template → Zone Resources: *Specific zone: example.com* only.

```bash
apt install -y certbot python3-certbot-dns-cloudflare

mkdir -p /root/.secrets
# /root/.secrets/cloudflare.ini  (chmod 600):
#   dns_cloudflare_api_token = <SCOPED_TOKEN>

certbot certonly --dns-cloudflare \
  --dns-cloudflare-credentials /root/.secrets/cloudflare.ini \
  -d office.example.com \
  --deploy-hook "systemctl reload apache2"
```

> **IPv6 gotcha:** the VM has no IPv6 route; certbot initially failed with `Network is unreachable` reaching the ACME API over an AAAA record. Fixed system-wide by preferring IPv4 in `/etc/gai.conf`:
> `precedence ::ffff:0:0/96  100`

---

## 5. Apache reverse proxy

### 5.1 Modules

```bash
a2enmod proxy proxy_http proxy_wstunnel rewrite headers ssl
```

### 5.2 Existing site — `/etc/apache2/sites-available/nextcloud.conf`

`ServerName cloud.example.com` was added as the first line inside the `<VirtualHost *:443>` block, making name-based vhost selection deterministic. This vhost remains first-loaded and therefore the **default** for port 443 (requests by bare IP — the `10.0.0.40` trusted-domain entries — still land on Nextcloud). It carries no `SSLCertificateFile` of its own; it inherits the global certificate from `mods-available/ssl.conf` (TurnKey pattern). The port-80 vhost (HTTPS redirect) must remain for dehydrated's HTTP-01 renewals.

### 5.3 New site — `/etc/apache2/sites-available/office.conf`

Full file in Appendix A. Key elements: per-vhost Let's Encrypt cert paths (per-vhost directives override the global default), HSTS, `ProxyPreserveHost On`, `X-Forwarded-Proto https`, a WebSocket upgrade rewrite to `ws://10.0.0.41/`, and `ProxyPass / http://10.0.0.41/` with a 300 s timeout.

```bash
a2ensite office && apachectl configtest && systemctl reload apache2
```

---

## 6. Nextcloud connector

Two distinct apps are involved:

| App ID | Version | Role |
|---|---|---|
| `office` | 1.0.0 | Nextcloud's office-suite **selector** ("Select your preferred office suite" page). Selecting the Euro-Office-powered option here does **not** install the connector. |
| `eurooffice` | 11.0.0 | The actual **connector** (ONLYOFFICE connector lineage, rebadged "Nextcloud Office" in the app store; NC 33–35). Provides the settings page and editor wiring. |

### 6.1 Installation

`occ app:install eurooffice` failed (cURL timeout to GitHub — the IPv6 issue again; PHP does not honour gai.conf the same way in all paths). Manual install:

```bash
cd /tmp
wget -4 https://github.com/nextcloud-releases/eurooffice/releases/download/v11.0.0/eurooffice-v11.0.0.tar.gz
tar -xzf eurooffice-v11.0.0.tar.gz -C /var/www/nextcloud/apps/
chown -R www-data:www-data /var/www/nextcloud/apps/eurooffice
occ app:enable eurooffice
```

### 6.2 Configuration (occ — values stored in app config)

```bash
occ config:app:set eurooffice DocumentServerUrl         --value="https://office.example.com/"
occ config:app:set eurooffice DocumentServerInternalUrl --value="http://10.0.0.41/"
occ config:app:set eurooffice StorageUrl                --value="https://cloud.example.com/"
occ config:app:set eurooffice jwt_secret                --value="<JWT_SECRET>"
```

Loading **Settings → Administration → Nextcloud Office** (`/settings/admin/office`) runs a live connection test (uploads a conversion check file) and reports the result.

> `occ` here is the wrapper at `/usr/local/bin/occ` — TurnKey ships no `sudo`, so the wrapper runs `su -s /bin/sh www-data -c "php occ …"` from `/var/www/nextcloud` (Appendix D).

### 6.3 config.php hygiene applied during this build

```bash
occ config:system:set overwrite.cli.url --value="https://cloud.example.com"
occ config:system:set default_phone_region --value="GB"
occ config:system:set log_rotate_size --value="104857600" --type=integer
```

---

## 7. Known quirks and gotchas

These cost real diagnostic time. Read before any upgrade or rebuild.

### Quirk 1 — `default.json` is the live config; `local.json` is ignored

The docservice/converter binaries are pkg snapshots run with `NODE_ENV=production-linux` and `NODE_CONFIG_DIR=/etc/euro-office/documentserver`, but **the config loader does not merge the `local*.json` overlays** in this build (9.3.1-0). Symptom: identical JWT secrets on both sides yet `checkJwt error: invalid signature`; the server was validating against the compiled-in default secret `"secret"`.

**Rule: every secret and setting change goes into `default.json`.** Mirror into `local.json` for forward compatibility, but never trust `local.json` alone. Verify which secret is live with `/root/jwttest.py` (Appendix C). Candidate for an upstream bug report.

### Quirk 2 — nginx `secure_link` secret mismatch → 403 on conversion check

The deb installer writes a random `secure_link_secret` into `/etc/euro-office/documentserver/nginx/ds.conf`, but docservice signs cache URLs with `secretString` from `default.json`, which ships as `verysecretstring`. Because of Quirk 1, the installer's attempt to align them never reaches the running service. Symptom: connector test fails with `403 Forbidden` on a `…/cache/files/data/conv_check_…` URL. Fix: one random value in `default.json` `secretString` **and** `ds.conf` **and** both `.tmpl` files (§3.4). The bundled `documentserver-update-securelink.sh` writes to `local.json` and is therefore ineffective here.

### Quirk 3 — IPv6 with no route breaks ACME and app-store installs

The Nextcloud VM has IPv6 enabled but no v6 path through the router. certbot and Nextcloud's app-store downloader both preferred AAAA records and failed. Mitigations: `gai.conf` IPv4 precedence (§4.3) and `wget -4` for manual fetches. A future option is disabling IPv6 on the VM via sysctl.

### Quirk 4 — TurnKey ships no `sudo`

`sudo -u www-data php occ …` fails. Use `su -s /bin/sh www-data -c "…"` or the `/usr/local/bin/occ` wrapper (Appendix D).

### Quirk 5 — Two apps, one confusing name

The app-store listing for the connector is titled "Nextcloud Office" with app ID `eurooffice`; the suite-selector app is ID `office`. `occ app:list | grep -iE 'office|euro'` disambiguates.

### Quirk 6 — `certbot renew` runs on the VM, not the LXC

No certificates exist on CT 141. Renewal checks belong on 10.0.0.40.

---

## 8. Operational runbook

### 8.1 Package update procedure (DocumentServer)

Package upgrades will likely **overwrite `default.json`** and revert secrets to defaults — silently breaking JWT and secure_link. Always follow this sequence:

```bash
# 1. Snapshot first (on pve)
pct snapshot 141 pre-upgrade-$(date +%Y%m%d)

# 2. Back up the live config (in CT 141)
cp -a /etc/euro-office/documentserver /root/ds-config-backup-$(date +%Y%m%d)

# 3. Upgrade
wget "https://github.com/Euro-Office/DocumentServer/releases/download/v<NEW>/euro-office-documentserver_<NEW>_amd64.deb" -O /tmp/eo.deb
apt install -y /tmp/eo.deb

# 4. RE-APPLY secrets to default.json (JWT ×3 slots + secretString) — §3.3/§3.4
# 5. Confirm nginx ds.conf secure_link_secret survived; re-align if not
# 6. Restart and verify
systemctl restart ds-docservice ds-converter && systemctl reload nginx
sleep 15 && python3 /root/jwttest.py        # local.json/default value -> error:0; "secret" -> error:6
curl http://localhost/healthcheck            # true
# 7. Open a document from Nextcloud; confirm save round-trip
```

### 8.2 Scheduled checks

| Cadence | Check | Where |
|---|---|---|
| Monthly | `certbot renew --dry-run` | Nextcloud VM |
| Monthly | `openssl x509 -in /etc/ssl/private/cert.pem -noout -dates` (dehydrated cert health) | Nextcloud VM |
| Monthly | `curl https://office.example.com/healthcheck` from outside the LAN | any external client |
| Monthly | `apt update && apt upgrade` (OS packages) | CT 141 + VM |
| Quarterly | Two-device co-edit test (Fedora + phone on 4G); save round-trip | — |
| Quarterly | External port scan of <WAN_IP> — only 80/443 should answer | mobile data |
| Quarterly | scan.nextcloud.com re-scan | — |

### 8.3 Backups

- **Action item:** add CT 141 to the cluster vzdump backup job (it is not covered by the schedule that protects the Nextcloud VM). The document server holds no user documents — files live in Nextcloud — so a config-level restore (CT backup + this runbook) is sufficient for DR.
- Secrets (JWT, secure_link, Cloudflare token) live in the password manager, not in documentation.

### 8.4 Reboot behaviour

`onboot=1`; all ds-* services and nginx are systemd-enabled. Verified: `pct reboot 141` → external healthcheck `true` without intervention. Note docservice takes ~10–20 s after start; a transient nginx 502 during that window is normal.

### 8.5 Security posture summary

1. JWT (HS256) enforced on inbox/outbox/browser; verified live with jwttest.py.
2. nginx `secure_link` on cache downloads, random secret.
3. TLS-only with HSTS (`max-age=15552000; includeSubDomains`) on both hostnames.
4. `ds-example` disabled (`systemctl status ds-example` → inactive/disabled).
5. Only 80/443 exposed at the router; router WAN-side admin interfaces off.
6. All secrets exposed during the build session (JWT, secure_link, SMTP) rotated on completion.
7. Cloudflare records DNS-only; API token zone-scoped, 0600 on disk.
8. External scan via scan.nextcloud.com on completion.

---

## 9. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| nginx 502 on `/healthcheck` | docservice still booting (10–20 s) or crash-looping | wait, then `ss -tlnp \| grep 8000`; logs under `/var/log/euro-office/documentserver/docservice/` |
| Editor: "document security token is not correctly formed" | JWT mismatch — almost certainly Quirk 1 (`default.json` reverted) | jwttest.py to identify the live secret; re-apply to `default.json`; restart ds services; hard-refresh browser (Ctrl+Shift+R) |
| Connector test: `403 Forbidden` on `…/cache/files/…` URL | secure_link mismatch (Quirk 2) or clock skew (URL carries `expires=`) | align `secretString` ↔ `ds.conf`; check `date` agrees on VM and CT |
| `occ app:install` cURL timeout to GitHub | IPv6 (Quirk 3) | manual install with `wget -4` (§6.1) |
| certbot "Network is unreachable" | IPv6 (Quirk 3) | `gai.conf` precedence line (§4.3) |
| certbot dry-run: "No simulated renewals were attempted" | running on the wrong host | run on the Nextcloud VM (Quirk 6) |
| Editor loads but changes never save | DS→Nextcloud callback failing | check `/etc/hosts` pinning on CT 141; outbox JWT slot; Apache access log on VM for `/apps/eurooffice/track` hits |
| Everything broken after `apt install` of new DS version | config overwritten | §8.1 step 4 onwards |

Definitive JWT diagnosis: `python3 /root/jwttest.py` asks the live server which secret it accepts via `CommandService.ashx` (`error:0` accepted / `error:6` rejected). Faster and more reliable than log archaeology.

---

## 10. Family usage

1. Each family member gets a Nextcloud account, all placed in a `family` group.
2. The **Group folders** app provides a shared "Family Documents" folder with edit rights for the group — one canonical location, collaborative editing works out of the box, no per-file shares to manage.
3. Sharing defaults (Settings → Sharing): passwords required on public links; default expiry set.
4. Mobile/desktop: standard Nextcloud apps; documents open in the Euro-Office editor in-app/in-browser with no client-side configuration.
5. Two people can edit the same document simultaneously; presence, cursors and change history are visible in the editor.

---

## 11. Explain like I'm 5

Nextcloud is the family's toy box where all the documents live. Euro-Office is a craft table that lives in its own room (the LXC) — when you want to draw on a document, the toy box hands it to the craft table, you and your siblings can all draw on it at the same time, and the table hands it back to the box when you're done. Both the toy box and the craft table check a secret handshake (JWT) before passing anything, so strangers on the internet can't slip drawings in. There's one front door to the house (port 443); a doorman (Apache) reads the name on each visitor's envelope and walks them to either the toy box or the craft table.

---

## 12. References

1. Euro-Office documentation — installation (Debian): https://euro-office.github.io/documentation/installation/debian/
2. Euro-Office documentation — Nextcloud integration: https://euro-office.github.io/documentation/integration/nextcloud/
3. Euro-Office DocumentServer releases: https://github.com/Euro-Office/DocumentServer/releases
4. Connector app source: https://github.com/Euro-Office/eurooffice-nextcloud (release tarballs: https://github.com/nextcloud-releases/eurooffice/releases)
5. certbot dns-cloudflare plugin: https://certbot-dns-cloudflare.readthedocs.io/
6. Nextcloud admin manual — reverse proxy / config.php parameters: https://docs.nextcloud.com/server/latest/admin_manual/
7. Apache mod_proxy_wstunnel: https://httpd.apache.org/docs/2.4/mod/mod_proxy_wstunnel.html

---

## 13. Appendices

### Appendix A — `/etc/apache2/sites-available/office.conf`

```apache
<VirtualHost *:443>
    ServerName office.example.com

    SSLEngine on
    SSLCertificateFile /etc/letsencrypt/live/office.example.com/fullchain.pem
    SSLCertificateKeyFile /etc/letsencrypt/live/office.example.com/privkey.pem

    Header always set Strict-Transport-Security "max-age=15552000; includeSubDomains"

    ProxyPreserveHost On
    RequestHeader set X-Forwarded-Proto "https"
    ProxyTimeout 300

    # WebSockets (collaborative editing)
    RewriteEngine On
    RewriteCond %{HTTP:Upgrade} websocket [NC]
    RewriteCond %{HTTP:Connection} upgrade [NC]
    RewriteRule ^/?(.*) "ws://10.0.0.41/$1" [P,L]

    ProxyPass        / http://10.0.0.41/
    ProxyPassReverse / http://10.0.0.41/
</VirtualHost>
```

### Appendix B — JWT structure in `default.json` / `local.json`

Effective location (Quirk 1): `/etc/euro-office/documentserver/default.json` →
`services.CoAuthoring.token.secret.{inbox,outbox,browser}.string` — all three identical. Token enablement: `services.CoAuthoring.token.enable.request.{inbox,outbox} = true`, `…enable.browser = true`. The secure_link signing key is `services.CoAuthoring` sibling key `secretString` (line ~110). `local.json` mirror (kept for forward compatibility):

```json
{
  "services": {
    "CoAuthoring": {
      "token": {
        "enable": {
          "request": { "inbox": true, "outbox": true },
          "browser": true
        },
        "secret": {
          "inbox":   { "string": "<JWT_SECRET>" },
          "outbox":  { "string": "<JWT_SECRET>" },
          "browser": { "string": "<JWT_SECRET>" }
        }
      }
    }
  }
}
```

### Appendix C — `/root/jwttest.py` (CT 141) — live JWT verification

```python
import hmac, hashlib, base64, json, urllib.request

def b64(d): return base64.urlsafe_b64encode(d).rstrip(b'=')
def jwt(payload, secret):
    h = b64(json.dumps({"alg":"HS256","typ":"JWT"}).encode())
    p = b64(json.dumps(payload).encode())
    s = b64(hmac.new(secret.encode(), h+b'.'+p, hashlib.sha256).digest())
    return (h+b'.'+p+b'.'+s).decode()

for label, sec in [("compiled-in default", "secret"),
                   ("configured secret", "<JWT_SECRET>")]:
    body = json.dumps({"c":"version","token":jwt({"c":"version"}, sec)}).encode()
    req = urllib.request.Request("http://localhost/coauthoring/CommandService.ashx",
                                 data=body, headers={"Content-Type":"application/json"})
    print(label, "->", urllib.request.urlopen(req).read().decode())
```

Expected when healthy: `compiled-in default -> {"error":6}` and `configured secret -> {"error":0,"version":"…"}`. Update `<JWT_SECRET>` in the script after every rotation.

### Appendix D — `/usr/local/bin/occ` wrapper (Nextcloud VM)

```bash
#!/bin/bash
cd /var/www/nextcloud
exec su -s /bin/sh www-data -c "/usr/bin/php occ $(printf '%q ' "$@")"
```

### Appendix E — Change log

| Date | Version | Change |
|---|---|---|
| 10 Jun 2026 | 1.0 | Initial build and documentation: CT 141 created; Euro-Office DS 9.3.1 installed; JWT + secure_link configured (Quirks 1–2 discovered and resolved); office.conf vhost + DNS-01 cert deployed; connector `eurooffice` 11.0.0 installed and configured; secrets rotated post-build. |

### Appendix F — Glossary

| Term | Meaning |
|---|---|
| **DocumentServer (DS)** | The Euro-Office backend that renders and co-ordinates document editing |
| **Connector** | The Nextcloud app (`eurooffice`) that wires Files to the DS |
| **JWT** | JSON Web Token — HS256-signed token authenticating every NC↔DS request |
| **inbox / outbox / browser** | The three JWT slots: requests *to* the DS, callbacks *from* the DS, and the editor config handed to the browser |
| **secure_link** | nginx module validating MD5-signed, expiring cache-download URLs |
| **DNS-01** | ACME challenge proven via a DNS TXT record (no port 80 needed) |
| **HTTP-01** | ACME challenge proven via a file served on port 80 |
| **SNI** | Server Name Indication — how Apache picks the right vhost/cert on a shared 443 |
| **Hairpin NAT** | LAN client reaching a LAN server via its WAN IP; avoided here via `/etc/hosts` |
| **HSTS** | Strict-Transport-Security header forcing HTTPS for the domain and subdomains |
| **pkg snapshot** | Node.js application compiled into a self-contained binary (vercel/pkg) — relevant because it embeds default config |

---

*End of document. Maintain via PR / commit; tag releases by date and DocumentServer version.*

#!/usr/bin/env bash
# Run on the WhatsApp MCP EC2 from the repo root: sudo LINKEDIN_DB_PASSWORD=... bash deploy/setup-server.sh
set -euo pipefail
: "${LINKEDIN_DB_PASSWORD:?set LINKEDIN_DB_PASSWORD}"
DOMAIN=${DOMAIN:-jobs.example.com}
SRC=$(cd "$(dirname "$0")/.." && pwd)

# Database objects (additive only)
for f in "$SRC"/db/00{1,2,3}_*.sql; do sudo -u postgres psql -d appdb -v ON_ERROR_STOP=1 -q -f "$f"; done
sudo -u postgres psql -d appdb -v ON_ERROR_STOP=1 -q -v pw="$LINKEDIN_DB_PASSWORD" <<'SQL'
ALTER ROLE linkedin_scraper PASSWORD :'pw';
ALTER TABLE linkedin.scraper_state OWNER TO appuser;
ALTER TABLE linkedin.scraper_events OWNER TO appuser;
SQL

# App code + venv
id jobsscrap >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin jobsscrap
mkdir -p /opt/jobsscrap
rm -rf /opt/jobsscrap/dashboard && cp -r "$SRC/dashboard" /opt/jobsscrap/dashboard
[ -x /opt/jobsscrap/venv/bin/pip ] || python3 -m venv /opt/jobsscrap/venv
/opt/jobsscrap/venv/bin/pip install -q -r /opt/jobsscrap/dashboard/requirements.txt
GIT=(git -c safe.directory="$SRC" -C "$SRC")
SHA=$("${GIT[@]}" rev-parse HEAD 2>/dev/null || true)
DIRTY=$([ -n "$SHA" ] && [ -n "$("${GIT[@]}" status --porcelain 2>/dev/null)" ] && echo 1 || true)
SHA="$SHA" DIRTY="$DIRTY" RUN_ID="${GITHUB_RUN_ID:-}" python3 -c 'import json,os,datetime as d; print(json.dumps({"sha": os.environ["SHA"], "dirty": bool(os.environ["DIRTY"]), "run_id": os.environ["RUN_ID"], "deployed_at": d.datetime.now(d.timezone.utc).isoformat()}))' \
  > /opt/jobsscrap/dashboard/build.json
chown -R root:root /opt/jobsscrap && chmod -R go-w /opt/jobsscrap

install -d -m 750 -o root -g jobsscrap /etc/jobsscrap
umask 027
printf 'DB_DSN=host=127.0.0.1 port=5432 dbname=appdb user=linkedin_scraper password=%s\n' "$LINKEDIN_DB_PASSWORD" > /etc/jobsscrap/env
chgrp jobsscrap /etc/jobsscrap/env && chmod 640 /etc/jobsscrap/env

# oauth2-proxy: same Google client and allowlist as DTMF, own cookie + port
if [ ! -f /etc/oauth2-proxy/jobsscrap.cfg ]; then
  COOKIE=$(python3 -c 'import secrets,base64;print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())')
  sed -E \
    -e 's#^http_address.*#http_address = "127.0.0.1:4183"#' \
    -e "s#^redirect_url.*#redirect_url = \"https://$DOMAIN/oauth2/callback\"#" \
    -e 's#^upstreams.*#upstreams = ["http://127.0.0.1:8096/"]#' \
    -e 's#^cookie_name.*#cookie_name = "_jobsscrap_oauth2"#' \
    -e "s#^cookie_secret.*#cookie_secret = \"$COOKIE\"#" \
    /etc/oauth2-proxy/dtmf.cfg > /etc/oauth2-proxy/jobsscrap.cfg
  chown root:oauth2proxy /etc/oauth2-proxy/jobsscrap.cfg && chmod 640 /etc/oauth2-proxy/jobsscrap.cfg
fi

# TLS: serve ACME over HTTP first, then install the full site
if [ ! -d /etc/letsencrypt/live/$DOMAIN ]; then
  printf 'server { listen 80; listen [::]:80; server_name %s; location ^~ /.well-known/acme-challenge/ { root /var/www/certbot; } }\n' "$DOMAIN" > /etc/nginx/sites-available/jobsscrap
  ln -sf /etc/nginx/sites-available/jobsscrap /etc/nginx/sites-enabled/jobsscrap
  nginx -t && systemctl reload nginx
  certbot certonly --webroot -w /var/www/certbot -d "$DOMAIN" --non-interactive --agree-tos --register-unsafely-without-email
fi
cp "$SRC/deploy/nginx-jobsscrap.conf" /etc/nginx/sites-available/jobsscrap
ln -sf /etc/nginx/sites-available/jobsscrap /etc/nginx/sites-enabled/jobsscrap

cp "$SRC/deploy/jobsscrap-dashboard.service" "$SRC/deploy/oauth2-proxy-jobsscrap.service" /etc/systemd/system/
install -o root -g root -m 755 "$SRC/deploy/jobsscrap-deploy.sh" /usr/local/bin/jobsscrap-deploy.sh
systemctl daemon-reload
systemctl enable --now jobsscrap-dashboard oauth2-proxy-jobsscrap
systemctl restart jobsscrap-dashboard oauth2-proxy-jobsscrap
nginx -t && systemctl reload nginx
echo "deployed https://$DOMAIN"

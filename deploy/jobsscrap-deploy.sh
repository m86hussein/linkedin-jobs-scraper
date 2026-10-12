#!/usr/bin/env bash
# Installed to /usr/local/bin by setup-server.sh so a repo change cannot rewrite the deployer.
# Run as root (via SSM from GitHub Actions): GITHUB_RUN_ID=... jobsscrap-deploy.sh
set -euo pipefail

SRC=/opt/jobsscrap/src
APP=/opt/jobsscrap/dashboard
export GIT_SSH_COMMAND="ssh -i /home/ubuntu/.ssh/jobsscrap-deploy -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"

git -C "$SRC" fetch --quiet origin main
git -C "$SRC" reset --quiet --hard origin/main
SHA=$(git -C "$SRC" rev-parse HEAD)

for f in "$SRC"/db/[0-9][0-9][0-9]_*.sql; do
  sudo -u postgres psql -d appdb -v ON_ERROR_STOP=1 -q -f "$f"
done

rm -rf "$APP.new"
cp -r "$SRC/dashboard" "$APP.new"
rm -rf "$APP.new/__pycache__"
SHA="$SHA" RUN_ID="${GITHUB_RUN_ID:-}" python3 -c 'import json,os,datetime as d; print(json.dumps({"sha": os.environ["SHA"], "dirty": False, "run_id": os.environ["RUN_ID"], "deployed_at": d.datetime.now(d.timezone.utc).isoformat()}))' \
  > "$APP.new/build.json"
/opt/jobsscrap/venv/bin/pip install -q -r "$APP.new/requirements.txt"
chown -R root:root "$APP.new" && chmod -R go-w "$APP.new"
rm -rf "$APP" && mv "$APP.new" "$APP"

# WhatsApp screenshot ingest reuses the agent's parser, screener and config.
WA=/opt/jobsscrap/whatsapp
rm -rf "$WA.new"
cp -r "$SRC/whatsapp" "$WA.new"
cp "$SRC/agent/parser.py" "$SRC/agent/titles.py" "$SRC/agent/config.json" "$WA.new/"
rm -rf "$WA.new/__pycache__"
/opt/jobsscrap/venv/bin/pip install -q -r "$WA.new/requirements.txt"
chown -R root:root "$WA.new" && chmod -R go-w "$WA.new"
rm -rf "$WA" && mv "$WA.new" "$WA"
install -o root -g root -m 644 "$SRC/deploy/jobsscrap-whatsapp-images.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable -q jobsscrap-whatsapp-images
systemctl restart jobsscrap-whatsapp-images

systemctl restart jobsscrap-dashboard
for _ in $(seq 1 15); do
  curl -fsS -o /dev/null http://127.0.0.1:8096/api/me && break
  sleep 1
done
curl -fsS -o /dev/null http://127.0.0.1:8096/api/me
echo "deployed ${SHA} run=${GITHUB_RUN_ID:-none}"

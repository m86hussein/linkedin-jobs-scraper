#!/bin/zsh
# Runs the agent as a launchd user agent: starts at login, restarts if it exits.
set -euo pipefail
LABEL=com.github.linkedin-jobs-scraper.agent
DIR=${0:A:h}
UV=$(command -v uv) || { echo "uv not found on PATH" >&2; exit 1; }
PLIST=~/Library/LaunchAgents/$LABEL.plist
LOG=~/Library/Logs/linkedin-agent.log

mkdir -p ~/Library/LaunchAgents ~/Library/Logs
cat > $PLIST <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/caffeinate</string><string>-i</string>
    <string>$UV</string><string>run</string><string>--with-requirements</string><string>requirements.txt</string>
    <string>python</string><string>agent.py</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>${UV:h}:/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>SSL_CERT_FILE</key><string>/etc/ssl/cert.pem</string>
    <key>PYTHONUNBUFFERED</key><string>1</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>60</integer>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
</dict>
</plist>
EOF

launchctl bootout gui/$UID/$LABEL 2>/dev/null || true
launchctl bootstrap gui/$UID $PLIST
echo "installed $LABEL; logs: $DIR/agent.log and $LOG"
echo "stop:    launchctl bootout gui/\$UID/$LABEL"

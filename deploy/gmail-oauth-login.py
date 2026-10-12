#!/usr/bin/env python3
"""One-time Gmail sign-in for the dashboard's Compose tab (run on your Mac).

Opens Google consent for gmail.send, then installs the refresh token on the server as
/etc/jobsscrap/gmail-oauth.json. Nothing secret is printed or written to local disk.

Tokens only stay valid long-term if the OAuth app is "In production"
(Google Cloud console > Google Auth Platform > Audience); in "Testing" they expire after 7 days.
"""
import argparse
import base64
import json
import os
import subprocess
import sys
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["openid", "https://www.googleapis.com/auth/userinfo.email", "https://www.googleapis.com/auth/gmail.send"]

ap = argparse.ArgumentParser()
ap.add_argument("--account", required=True, help="Gmail address to send from")
ap.add_argument("--client", default=str(Path.home() / ".config/gmail-cli/credentials.json"), help="Desktop OAuth client JSON")
ap.add_argument("--host", required=True, help="SSH target of the dashboard server, e.g. ubuntu@your-server")
ap.add_argument("--key", default=str(Path.home() / ".ssh/id_ed25519"))
args = ap.parse_args()

os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = "1"  # Google lets the user untick scopes; check below instead
flow = InstalledAppFlow.from_client_secrets_file(args.client, SCOPES)
creds = flow.run_local_server(port=0, login_hint=args.account, prompt="consent", access_type="offline",
                              open_browser=True, authorization_prompt_message="Approve in the browser: {url}\n")
granted = flow.oauth2session.token.get("scope") or []
if SCOPES[2] not in (granted.split() if isinstance(granted, str) else granted):
    sys.exit('The "Send email on your behalf" box was not ticked on the consent screen. Run again and tick it.')
payload = creds.id_token.split(".")[1]
email = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))).get("email", "")
if email.lower() != args.account.lower():
    sys.exit(f"Signed in as {email!r}, expected {args.account}; nothing installed.")
if not creds.refresh_token:
    sys.exit("Google returned no refresh token; nothing installed.")

doc = json.dumps({"type": "authorized_user", "account": email, "client_id": creds.client_id,
                  "client_secret": creds.client_secret, "refresh_token": creds.refresh_token})
subprocess.run(["ssh", "-i", args.key, args.host,
                "sudo install -m 640 -o root -g jobsscrap /dev/stdin /etc/jobsscrap/gmail-oauth.json"],
               input=doc.encode(), check=True)
print(f"Installed Gmail sign-in for {email} on {args.host}. Refresh the dashboard; Send is enabled.")

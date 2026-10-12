"""LinkedIn post screenshots sent on WhatsApp -> linkedin.job_posts.

The WhatsApp bridge POSTs each downloaded incoming image to this service (MEDIA_HOOK_URL). Images from
WATCH_PHONE are read with Claude (vision), screened like scraped posts, saved, and answered on WhatsApp.
"""
import json
import logging
import os
import queue
import re
import sqlite3
import threading
import time
import urllib.request
from contextlib import closing
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from parser import EMAIL_RE, SEEKER_RE, Post, _posted, content_hash
from titles import PostScreener

CFG = json.loads(Path(__file__).with_name("config.json").read_text(encoding="utf-8"))
STORE = Path(os.environ.get("BRIDGE_STORE", "/home/ubuntu/whatsapp-mcp/whatsapp-bridge/store"))
BRIDGE = os.environ.get("BRIDGE_URL", "http://127.0.0.1:8080")
PHONE = os.environ["WATCH_PHONE"]
LOOKBACK = timedelta(hours=float(os.environ.get("LOOKBACK_HOURS", "24")))
HOOK_PORT = int(os.environ.get("HOOK_PORT", "8097"))
MAX_ATTEMPTS = 3
SOURCE = "WhatsApp screenshot"
URL_RE = re.compile(r"https?://(?:[\w-]+\.)?linkedin\.com/[^\s<>\"']+")
FORMATS = {b"\xff\xd8": "jpeg", b"\x89P": "png", b"GI": "gif", b"RI": "webp"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("linkedin-images")

READ_SYSTEM = (
    "You read a screenshot that should show a LinkedIn post (usually a phone screenshot). Transcribe the post text "
    "exactly as shown, in its original language, keeping line breaks, emojis and hashtags. Leave out LinkedIn interface "
    "text: buttons, reaction and comment counts, '…more', ads, other posts and the phone status bar. "
    "posted_ago is the post age or date shown under the author, in LinkedIn's short form (e.g. '5h', '3d', '2w', "
    "'1mo', '1yr', 'Sep 28'). If the post has a job card, copy its title, company and location. "
    "Copy any linkedin.com URL that is visible.")
_S = {"type": "string"}
READ_TOOL = {"toolSpec": {
    "name": "record_post",
    "description": "Record the LinkedIn post shown in the screenshot.",
    "inputSchema": {"json": {"type": "object", "required": ["is_linkedin_post"], "properties": {
        "is_linkedin_post": {"type": "boolean", "description": "The image shows a LinkedIn post"},
        "author_name": _S, "author_headline": _S, "posted_ago": _S,
        "content": {"type": "string", "description": "The post text"},
        "truncated": {"type": "boolean", "description": "The text is cut off ('…more') or continues past the screenshot"},
        "job_title": _S, "company": _S, "location": _S, "post_url": _S, "job_url": _S,
    }}},
}}


def bridge(path: str, body: dict) -> dict:
    req = urllib.request.Request(f"{BRIDGE}{path}", json.dumps(body).encode(), {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def watched_chats() -> list[str]:
    # The bridge files newer chats under the sender's LID, not the phone number.
    with closing(sqlite3.connect(f"file:{STORE / 'whatsapp.db'}?mode=ro", uri=True)) as c:
        lids = [r[0] for r in c.execute("SELECT lid FROM whatsmeow_lid_map WHERE pn = ?", (PHONE,))]
    return [f"{PHONE}@s.whatsapp.net", *(f"{lid}@lid" for lid in lids)]


def recent_images(chats: list[str]) -> list[dict]:
    # Startup catch-up only: images that arrived while this service was down.
    since = (datetime.now(timezone.utc) - LOOKBACK).strftime("%Y-%m-%d %H:%M:%S")
    with closing(sqlite3.connect(f"file:{STORE / 'messages.db'}?mode=ro", uri=True)) as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            f"""SELECT id, chat_jid, content, timestamp FROM messages
                WHERE media_type = 'image' AND NOT is_from_me AND timestamp >= ?
                  AND chat_jid IN ({",".join("?" * len(chats))}) ORDER BY timestamp""", (since, *chats)).fetchall()
    return [dict(r) for r in rows]


def image(m: dict) -> tuple[bytes, str]:
    path = m.get("path")
    if not path:
        r = bridge("/api/download", {"message_id": m["id"], "chat_jid": m["chat_jid"]})
        if not r.get("success"):
            raise RuntimeError(f"bridge download failed: {r.get('message')}")
        path = r["path"]
    if not Path(path).resolve().is_relative_to(STORE.resolve()):
        raise RuntimeError("image path is outside the bridge store")
    data = Path(path).read_bytes()
    fmt = FORMATS.get(data[:2])
    if not fmt:
        raise RuntimeError("unsupported image format")
    return data, fmt


def read_post(client, model: str, data: bytes, fmt: str, caption: str) -> dict:
    content = [{"image": {"format": fmt, "source": {"bytes": data}}}]
    if caption:
        content.append({"text": f"Caption sent with the screenshot: {caption}"})
    r = client.converse(
        modelId=model, system=[{"text": READ_SYSTEM}], messages=[{"role": "user", "content": content}],
        toolConfig={"tools": [READ_TOOL], "toolChoice": {"tool": {"name": "record_post"}}},
        inferenceConfig={"maxTokens": 4000})
    for c in r["output"]["message"]["content"]:
        if "toolUse" in c:
            return c["toolUse"]["input"]
    raise RuntimeError("model returned no post")


def to_post(v: dict, caption: str, sent_at: datetime) -> Post:
    text = v["content"].strip()

    def clean(k):
        return str(v.get(k) or "").strip() or None

    def url(k):
        u = clean(k)
        return u if u and URL_RE.fullmatch(u) else None

    # _posted() reads the "3d •" / "Sep 28 •" line LinkedIn shows under the author.
    posted = _posted(f"{clean('posted_ago')} •", sent_at) if clean("posted_ago") else None
    return Post(content_hash=content_hash(text), content=text, author_name=clean("author_name"),
                author_headline=clean("author_headline"),
                post_url=url("post_url") or next(iter(URL_RE.findall(caption)), None),
                posted_ago=posted[0] if posted else clean("posted_ago"), posted_at_est=posted[1] if posted else None,
                job_title=clean("job_title"), company=clean("company"), location=clean("location"), job_url=url("job_url"),
                contact_emails=sorted({e.rstrip(".").lower() for e in EMAIL_RE.findall(text)}),
                is_hiring=not SEEKER_RE.search(text))


def save(db, p: Post) -> dict:
    return db.execute(
        """INSERT INTO linkedin.job_posts (content_hash, post_url, author_name, author_headline, posted_ago, posted_at_est,
               content, job_title, company, location, job_url, contact_emails, search_keywords, is_hiring)
           VALUES (%(content_hash)s, %(post_url)s, %(author_name)s, %(author_headline)s, %(posted_ago)s, %(posted_at_est)s,
               %(content)s, %(job_title)s, %(company)s, %(location)s, %(job_url)s, %(contact_emails)s, %(q)s, %(is_hiring)s)
           ON CONFLICT (content_hash) DO UPDATE SET last_seen_at = now(),
               post_url = COALESCE(linkedin.job_posts.post_url, EXCLUDED.post_url),
               job_url = COALESCE(linkedin.job_posts.job_url, EXCLUDED.job_url)
           RETURNING id, (xmax = 0) AS inserted, first_seen_at, ai_checked_at, ai_fit, ai_reason""",
        {**p.__dict__, "q": SOURCE}).fetchone()


def screen(db, screener: PostScreener, post_id: int, content: str) -> dict:
    v = screener(content)
    db.execute(
        """UPDATE linkedin.job_posts SET ai_offer = %(offer)s, ai_fit = %(fit)s, ai_reason = %(reason)s,
               ai_checked_at = now(), ai_model = %(model)s,
               ai_family = %(family)s, ai_sector = %(sector)s, ai_seniority = %(seniority)s,
               ai_sal_min = %(sal_min)s, ai_sal_max = %(sal_max)s, ai_sal_currency = %(sal_currency)s,
               ai_sal_period = %(sal_period)s, ai_sal_text = %(sal_text)s,
               ai_est_min_usd = %(est_min)s, ai_est_max_usd = %(est_max)s,
               ai_title = CASE WHEN job_title IS NULL AND ai_title IS NULL THEN %(title)s ELSE ai_title END
           WHERE id = %(id)s""", {**v, "id": post_id})
    return v


def summary(p: Post, row: dict, verdict: dict | None, truncated: bool) -> str:
    lines = [f"Saved LinkedIn post #{row['id']}"]
    lines.append(" - ".join(x for x in (p.author_name, p.author_headline) if x) or "Author not visible")
    if p.posted_ago:
        lines.append(f"Published: {p.posted_ago}" + (f" (~{p.posted_at_est:%d %b %Y})" if p.posted_at_est else ""))
    role = " @ ".join(x for x in (p.job_title or (verdict or {}).get("title", "").replace("\n", ", "), p.company) if x)
    if role:
        lines.append(f"Role: {role}" + (f", {p.location}" if p.location else ""))
    if p.contact_emails:
        lines.append("Email: " + ", ".join(p.contact_emails))
    if verdict:
        lines.append(("Fits" if verdict["fit"] else "Not a fit") + f": {verdict['reason']}")
    if truncated:
        lines.append("Text is cut off in the screenshot; only the visible part was saved.")
    return "\n".join(lines)


def handle(db, screener: PostScreener, m: dict) -> tuple[str, int | None, str]:
    sent_at = datetime.fromisoformat(m["timestamp"])
    caption = (m["content"] or "").strip()
    data, fmt = image(m)
    v = read_post(screener.client, screener.model, data, fmt, caption)
    if not v.get("is_linkedin_post") or not str(v.get("content") or "").strip():
        return "not_a_post", None, "That image doesn't look like a LinkedIn post, so nothing was saved."
    p = to_post(v, caption, sent_at)
    row = save(db, p)
    if not row["inserted"]:
        return "duplicate", row["id"], f"Already saved as post #{row['id']} (first seen {row['first_seen_at']:%d %b %Y})."
    verdict = screen(db, screener, row["id"], p.content) if p.is_hiring else None
    db.execute("INSERT INTO linkedin.scraper_events (level, message) VALUES ('info', %s)",
               (f"WhatsApp screenshot saved as post #{row['id']} ({p.author_name or 'unknown author'})",))
    return "saved", row["id"], summary(p, row, verdict, bool(v.get("truncated")))


def main():
    tm = CFG["title_model"]
    screener = PostScreener(tm["model"], None, tm["aws_region"], CFG["match_criteria"])
    db = psycopg.connect(os.environ["DB_DSN"], autocommit=True, row_factory=dict_row)
    events: queue.Queue = queue.Queue()
    for m in recent_images(watched_chats()):
        events.put(m)

    class Hook(BaseHTTPRequestHandler):
        def do_POST(self):
            m = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            self.send_response(204)
            self.end_headers()
            if (m.get("media_type") == "image" and m.get("is_from_me") != "true"
                    and m.get("chat_jid") in watched_chats()):
                events.put(m)

        def log_message(self, *args):
            pass

    def worker():
        try:
            while True:
                process(db, screener, events.get())
        except Exception:
            log.exception("worker died")
            os._exit(1)

    threading.Thread(target=worker, daemon=True).start()
    log.info("listening on 127.0.0.1:%d for images from %s", HOOK_PORT, PHONE)
    HTTPServer(("127.0.0.1", HOOK_PORT), Hook).serve_forever()


def process(db, screener: PostScreener, m: dict):
    if db.execute("SELECT 1 FROM linkedin.whatsapp_images WHERE message_id = %s", (m["id"],)).fetchone():
        return
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            status, post_id, reply = handle(db, screener, m)
            break
        except Exception as e:
            log.warning("image %s attempt %d failed: %s", m["id"], attempt, e)
            status, post_id, reply = "failed", None, f"Couldn't read that screenshot: {str(e)[:200]}"
            if attempt < MAX_ATTEMPTS:
                time.sleep(30 * attempt)
    db.execute(
        """INSERT INTO linkedin.whatsapp_images (message_id, chat_jid, received_at, status, post_id, detail)
           VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING""",
        (m["id"], m["chat_jid"], datetime.fromisoformat(m["timestamp"]), status, post_id, reply))
    log.info("image %s -> %s %s", m["id"], status, post_id or "")
    try:
        bridge("/api/send", {"recipient": PHONE, "message": reply})
    except Exception as e:
        log.warning("reply failed: %s", e)


if __name__ == "__main__":
    main()

import html
import re
import ssl
import urllib.request
from datetime import datetime

from parser import EMAIL_RE, SEEKER_RE, Post, content_hash

# Public channel preview (t.me/s/<channel>): no login or API key, ~20 newest messages per page.
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15"}
SSL = ssl.create_default_context(cafile="/etc/ssl/cert.pem")
BLOCK_RE = re.compile(r'<div class="tgme_widget_message [^"]*" data-post="([\w/]+)"(.*?)(?=<div class="tgme_widget_message_wrap|\Z)', re.S)
TEXT_RE = re.compile(r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', re.S)
TIME_RE = re.compile(r'<time datetime="([^"]+)"')
TITLE_RE = re.compile(r'<meta property="og:title" content="([^"]*)"')


def _text(fragment: str) -> str:
    fragment = re.sub(r"<br\s*/?>", "\n", fragment)
    fragment = re.sub(r"<i class=\"emoji\"[^>]*><b>(.*?)</b></i>", r"\1", fragment)
    return html.unescape(re.sub(r"<[^>]+>", "", fragment)).strip()


def fetch(channel: str, before: int | None = None) -> tuple[str, list[dict]]:
    url = f"https://t.me/s/{channel}" + (f"?before={before}" if before else "")
    page = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30, context=SSL).read().decode("utf-8", "replace")
    title = html.unescape(m.group(1)) if (m := TITLE_RE.search(page)) else channel
    out = []
    for post, body in BLOCK_RE.findall(page):
        text, at = TEXT_RE.search(body), TIME_RE.search(body)
        if text and at:
            out.append({"id": int(post.split("/")[1]), "url": f"https://t.me/{post}", "text": _text(text.group(1)),
                        "at": datetime.fromisoformat(at.group(1))})
    return title, out


def to_post(channel: str, title: str, m: dict) -> Post:
    text = m["text"]
    return Post(content_hash=content_hash(text), content=text, author_name=title, author_headline=f"Telegram @{channel}",
                author_profile_url=f"https://t.me/{channel}", post_url=m["url"], posted_ago=f"{m['at']:%b %d}",
                posted_at_est=m["at"], contact_emails=sorted({e.rstrip(".").lower() for e in EMAIL_RE.findall(text)}),
                is_hiring=not SEEKER_RE.search(text))

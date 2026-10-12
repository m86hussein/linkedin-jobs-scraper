import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

DEGREE_RE = re.compile(r"•\s*(1st|2nd|3rd\+)\s*$")
AGO_RE = re.compile(r"^(\d+)\s*(m|h|d|w|mo|yr)\b")
# Posts older than about a week show a date instead: "Sep 28 •", "Dec 3, 2025 •".
DATE_RE = re.compile(r"^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) (\d{1,2})(?:, (\d{4}))?\s*•")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
SEEKER_RE = re.compile(r"#?open\s*to\s*work|actively looking for|looking for (a |an )?(new |immediate )?(job|opportunit|role)", re.I)
ARABIC_HIRING = "|".join(["مطلوب", "توظيف", "وظيف", "وظائف", "فرصة عمل", "فرص عمل"])
# Bare "hiring" also appears in news and career tips; require a hiring phrase or a job card.
HIRING_RE = re.compile(
    r"\b(?:we(?:'|’)?re|we are|i(?:'|’)?m|i am|is|are|now|urgent(?:ly)?)\s+(?:actively\s+)?hiring\b|^\W*hiring\b|#hiring(?:now)?\b"
    r"|\bwe(?:'|’)?re (?:looking|searching) for\b|\bwe are (?:looking|searching) for\b|\bis looking for an?\b"
    r"|\bjoin (?:our|the) team\b|\bjob (?:opening|id)\b|\bopen (?:role|position)s?\b|\bvacanc(?:y|ies)\b"
    r"|\bapply (?:now|here|via|today)\b|\bsend (?:your |me your )?(?:cv|resume)\b|\bsharing (?:this|a) role\b"
    r"|\bcheck out this job\b|" + ARABIC_HIRING,
    re.I | re.M)
ACTION_LINES = {"Follow", "Connect", "Join", "Visit my website", "Subscribe"}
FOOTER_START = ("Like", "Are these results helpful?")
AGO_UNITS = {"m": timedelta(minutes=1), "h": timedelta(hours=1), "d": timedelta(days=1),
             "w": timedelta(weeks=1), "mo": timedelta(days=30), "yr": timedelta(days=365)}


@dataclass
class Post:
    content_hash: str
    content: str
    author_name: str | None = None
    author_headline: str | None = None
    author_profile_url: str | None = None
    post_url: str | None = None
    posted_ago: str | None = None
    posted_at_est: datetime | None = None
    job_title: str | None = None
    company: str | None = None
    location: str | None = None
    job_url: str | None = None
    contact_emails: list[str] = field(default_factory=list)
    is_hiring: bool = True


def content_hash(text: str) -> str:
    return hashlib.sha256(re.sub(r"[^a-z0-9]+", "", text.lower()).encode()).hexdigest()


def name_key(name: str) -> str:
    # Reference text and the rendered name differ in emoji, accents, punctuation and trailing badges.
    name = unicodedata.normalize("NFKD", name or "").casefold()
    return re.sub(r"[^a-z0-9]+", "", re.sub(r"(verified profile|premium|•.*)$", "", name))


def find_profile(people: dict[str, str], author: str | None) -> str | None:
    key = name_key(author or "")
    if len(key) < 3:
        return None
    if key in people:
        return people[key]
    hits = {url for k, url in people.items() if k.startswith(key) or key.startswith(k) and len(k) >= 3}
    return hits.pop() if len(hits) == 1 else None


def _posted(line: str, now: datetime) -> tuple[str, datetime] | None:
    if m := AGO_RE.match(line):
        return m.group(0), now - int(m.group(1)) * AGO_UNITS[m.group(2)]
    if m := DATE_RE.match(line):
        at = datetime.strptime(f"{m.group(1)} {m.group(2)} {m.group(3) or now.year}", "%b %d %Y").replace(tzinfo=timezone.utc)
        if not m.group(3) and at > now:
            at = at.replace(year=at.year - 1)
        return f"{m.group(1)} {m.group(2)}" + (f", {m.group(3)}" if m.group(3) else ""), at
    return None


def _parse_block(lines: list[str], now: datetime) -> Post | None:
    deg = next((i for i, l in enumerate(lines) if DEGREE_RE.search(l)), None)
    if deg is None:
        return None
    before = DEGREE_RE.sub("", lines[deg]).strip(" •")
    author = before or (lines[deg - 1] if deg > 0 else None)
    author = re.sub(r"\s*Verified Profile.*$", "", author or "").strip() or None

    ago_idx = next((i for i in range(deg + 1, min(deg + 6, len(lines))) if _posted(lines[i], now)), None)
    if ago_idx is None:
        return None
    headline = next((l for l in lines[deg + 1:ago_idx] if l not in ACTION_LINES), None)
    posted_ago, posted_at = _posted(lines[ago_idx], now)

    body_start = ago_idx + 1
    while body_start < len(lines) and lines[body_start] in ACTION_LINES:
        body_start += 1
    body_end = next((i for i in range(body_start, len(lines)) if lines[i] in ("… more", "...more") or lines[i].startswith(FOOTER_START)), len(lines))
    content = "\n".join(lines[body_start:body_end]).strip()
    if not content:
        return None

    post = Post(content_hash=content_hash(content), content=content, author_name=author,
                author_headline=headline, posted_ago=posted_ago, posted_at_est=posted_at)

    view_job = next((i for i in range(body_end, len(lines)) if lines[i] == "View job"), None)
    if view_job is not None and view_job >= 3:
        post.job_title = lines[view_job - 3].replace("(Verified job)", "").strip()
        post.company, post.location = lines[view_job - 2], lines[view_job - 1]

    post.contact_emails = sorted({e.rstrip(".").lower() for e in EMAIL_RE.findall(content)})
    post.is_hiring = bool(post.job_title or HIRING_RE.search(content)) and not SEEKER_RE.search(content)
    return post


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", re.sub(r"['’]", "", text.lower())).strip("-")


def slug_words(url: str) -> list[str]:
    # /posts/<author>_<first-words-of-post>-share-123-abcd -> the post words
    words = re.sub(r"-(?:share|ugcPost|activity)-\d+-[\w-]+$", "", url.rstrip("/").split("/")[-1]).split("_")[-1]
    return [w for w in words.lower().split("-") if w]


def slug_matches(url: str, content: str) -> bool:
    words = slug_words(url)
    return len(words) >= 10 and slugify(content).startswith("-".join(words))


def slug_consistent(url: str, content: str) -> bool:
    # Short slugs can't prove a match, but a word missing from the text proves a mismatch.
    text = slugify(content).replace("-", "")
    return all(w in text for w in slug_words(url))


def parse_search_results(result: dict, now: datetime | None = None, section: str = "search_results") -> list[Post]:
    now = now or datetime.now(timezone.utc)
    text = result.get("sections", {}).get(section, "")
    refs = result.get("references", {}).get(section, [])
    people = {name_key(r.get("text", "")): r["url"] for r in refs if r.get("kind") == "person" and name_key(r.get("text", ""))}
    post_links = [r["url"] for r in refs if r.get("kind") == "feed_post" and r["url"].startswith("/posts/")]
    # A job card's reference follows its author's reference.
    job_by_profile, last_person = {}, None
    for r in refs:
        if r.get("kind") == "person":
            last_person = r["url"]
        elif r.get("kind") == "job" and last_person:
            job_by_profile.setdefault(last_person, r["url"])

    posts, seen, used = [], set(), set()
    for block in text.split("Feed post")[1:]:
        lines = [l.strip() for l in block.splitlines() if l.strip()]
        post = _parse_block(lines, now)
        if not post or post.content_hash in seen:
            continue
        seen.add(post.content_hash)
        profile = find_profile(people, post.author_name)
        if profile:
            post.author_profile_url = "https://www.linkedin.com" + profile
            slug = profile.strip("/").split("/")[-1]
            # Frequent posters have several posts per page; don't hand them all the first link.
            mine = [u for u in post_links if u.startswith(f"/posts/{slug}_") and u not in used]
            match = next((u for u in mine if slug_matches(u, post.content)), None) or \
                next((u for u in mine if slug_consistent(u, post.content)), None)
            if match:
                used.add(match)
                post.post_url = "https://www.linkedin.com" + match
            if post.job_title and profile in job_by_profile:
                post.job_url = "https://www.linkedin.com" + job_by_profile[profile]
        if not post.post_url:
            match = next((u for u in post_links if u not in used and slug_matches(u, post.content)), None)
            if match:
                used.add(match)
                post.post_url = "https://www.linkedin.com" + match
        posts.append(post)
    return posts


def parse_people(result: dict) -> list[dict]:
    # People search: "Name • 2nd" (or name then "• 2nd"), headline, location. Only results with a profile reference.
    refs = result.get("references", {}).get("search_results", [])
    people = {name_key(r.get("text", "")): r["url"] for r in refs
              if r.get("kind") == "person" and r.get("url", "").startswith("/in/") and name_key(r.get("text", ""))}
    lines = [l.strip() for l in result.get("sections", {}).get("search_results", "").splitlines() if l.strip()]
    out = []
    for i, line in enumerate(lines):
        if not DEGREE_RE.search(line) or i + 2 >= len(lines):
            continue
        name = DEGREE_RE.sub("", line).strip() or (lines[i - 1] if i else "")
        profile = find_profile(people, name)
        if profile:
            out.append({"name": name, "url": "https://www.linkedin.com" + profile,
                        "headline": lines[i + 1], "location": lines[i + 2]})
    return out

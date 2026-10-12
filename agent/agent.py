import asyncio
import ipaddress
import json
import logging
import os
import random
import re
import socket
import subprocess
import time
import urllib.parse
import urllib.request
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from parser import parse_people, parse_search_results
from titles import CvMatcher, PostScreener
import telegram

HERE = Path(__file__).parent


def load_cfg() -> dict:
    cfg = json.loads((HERE / "config.json").read_text(encoding="utf-8-sig"))
    # Per-machine overrides (ssh user/key, agent_name, db password file); not committed.
    local = HERE / "config.local.json"
    if local.exists():
        for k, v in json.loads(local.read_text(encoding="utf-8-sig")).items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    return cfg


CFG = load_cfg()
TZ = ZoneInfo(CFG["timezone"])
CAIRO = ZoneInfo("Africa/Cairo")
# Read-only by design: the agent must never message, connect, post or react.
ALLOWED_TOOLS = {"search_posts", "get_feed", "get_person_profile", "search_people", "get_job_details"}
FEED_LABEL = "(home feed)"
PROFILE_LABEL = "(recruiter profile)"
HOST = CFG.get("agent_name") or socket.gethostname()
LEASE_SECONDS = 90
SEARCH_CONFIG_KEYS = ("queries", "date_posted", "max_pages", "gap_seconds", "test_gap_seconds",
                      "cycle_every_hours", "cycle_jitter_minutes", "max_searches_per_day", "searches_per_cycle", "active_hours", "timezone",
                      "feed_browse", "profile_visits", "recruiter_search", "job_details", "match_criteria")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    handlers=[logging.FileHandler(HERE / "agent.log", encoding="utf-8"), logging.StreamHandler()])
log = logging.getLogger("agent")


def role_pattern() -> re.Pattern:
    return re.compile("|".join(CFG["feed_browse"]["roles"]), re.I)


def profile_username(url: str) -> str:
    return urllib.parse.unquote(url.rstrip("/").split("/")[-1])


MCP_HOME = Path.home() / ".linkedin-mcp"
SESSION_FILES = ("profile", "cookies.json", "source-state.json")


def online() -> bool:
    try:
        socket.create_connection(("www.linkedin.com", 443), timeout=5).close()
        return True
    except OSError:
        return False


def network_dropped_session() -> Path | None:
    """The session backup the LinkedIn tool just made because a page failed to load, not because LinkedIn logged us out."""
    cutoff = time.time() - 900
    logs = sorted((p for p in MCP_HOME.glob("trace-runs/*/server.log") if p.stat().st_mtime > cutoff),
                  key=lambda p: p.stat().st_mtime)
    checks = [l for l in logs[-1].read_text(errors="ignore").splitlines() if "auth check failed" in l] if logs else []
    if not checks or not re.search(r"net::ERR_|TimeoutError", checks[-1]):
        return None
    backups = sorted(d for d in MCP_HOME.glob("invalid-state-*")
                     if d.stat().st_mtime > cutoff and (d / "profile").is_dir() and (d / "source-state.json").exists())
    return backups[-1] if backups else None


def restore_session(backup: Path):
    try:  # the login window the tool opened still holds the profile
        subprocess.run(["pkill", "-f", str(MCP_HOME / "profile")], check=False)
    except FileNotFoundError:
        pass
    aside = MCP_HOME / f"abandoned-login-{datetime.now(timezone.utc):%Y-%m-%dT%H-%M-%SZ}"
    aside.mkdir()
    for name in SESSION_FILES:
        if (MCP_HOME / name).exists():
            (MCP_HOME / name).rename(aside / name)
        if (backup / name).exists():
            (backup / name).rename(MCP_HOME / name)
    backup.rename(MCP_HOME / f"restored-{backup.name}")


class Blocked(Exception):
    pass


class LeaseLost(RuntimeError):
    pass


def public_ip() -> str | None:
    try:
        ip = urllib.request.urlopen("https://checkip.amazonaws.com", timeout=5).read().decode().strip()
        return str(ipaddress.ip_address(ip))
    except (OSError, ValueError):
        return None


def ip_info() -> dict:
    try:
        d = json.loads(urllib.request.urlopen("https://ipinfo.io/json", timeout=5).read())
        country = d.get("country") if re.fullmatch(r"[A-Z]{2}", d.get("country") or "") else None
        return {"ip": str(ipaddress.ip_address(d["ip"])), "country": country,
                "city": (d.get("city") or "")[:80] or None, "org": (d.get("org") or "")[:120] or None}
    except (OSError, ValueError, KeyError):
        return {"ip": public_ip(), "country": None, "city": None, "org": None}


def start_tunnel() -> subprocess.Popen:
    ssh = CFG["ssh"]
    port = ssh["local_port"]
    proc = subprocess.Popen(
        ["ssh", "-N", "-i", os.path.expanduser(ssh["key"]), "-o", "BatchMode=yes",
         "-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=30", "-o", "StrictHostKeyChecking=accept-new",
         "-L", f"{port}:127.0.0.1:5432", ssh["host"]],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(30):
        if proc.poll() is not None:
            raise RuntimeError(f"ssh tunnel exited with code {proc.returncode}")
        try:
            socket.create_connection(("127.0.0.1", port), timeout=1).close()
            return proc
        except OSError:
            time.sleep(1)
    proc.kill()
    raise RuntimeError("ssh tunnel did not come up")


def db_password() -> str:
    db = CFG["db"]
    if os.environ.get("LINKEDIN_DB_PASSWORD"):
        return os.environ["LINKEDIN_DB_PASSWORD"].strip()
    if db.get("password_file"):
        return Path(os.path.expanduser(db["password_file"])).read_text(encoding="utf-8-sig").strip()
    import boto3
    sm = boto3.Session(profile_name=db["aws_profile"], region_name=db["aws_region"]).client("secretsmanager")
    return sm.get_secret_value(SecretId=db["secret_id"])["SecretString"].strip()


class Db:
    # Async psycopg can't run on Windows' Proactor loop, which the MCP subprocess needs; use sync in threads.
    def __init__(self, conn: psycopg.Connection):
        self.conn = conn

    async def execute(self, sql: str, params=None):
        await asyncio.to_thread(self.conn.execute, sql, params)

    async def fetchone(self, sql: str, params=None) -> dict | None:
        return await asyncio.to_thread(lambda: self.conn.execute(sql, params).fetchone())

    async def fetchall(self, sql: str, params=None) -> list[dict]:
        return await asyncio.to_thread(lambda: self.conn.execute(sql, params).fetchall())


class Agent:
    def __init__(self, conn: Db):
        self.conn = conn
        self.desired = "stopped"
        self.lease_lost = False
        self.next_feed_at = self.feed_due()
        tm = CFG["title_model"]
        self.titles = PostScreener(tm["model"], CFG["db"].get("aws_profile"), tm["aws_region"], CFG["match_criteria"])
        self.matcher = CvMatcher(self.titles.client, tm["model"])
        self.titles_warned = False
        self.screen_lock = asyncio.Lock()
        self.restored_at = 0.0
        self.offline = False

    async def fill_titles(self) -> int:
        # Haiku screening (job offer? fits the criteria?) plus a title; also backfills older posts a batch at a time.
        async with self.screen_lock:
            rows = await self.conn.fetchall(
                """SELECT id, content FROM linkedin.job_posts
                   WHERE is_hiring AND (ai_checked_at IS NULL OR ai_model IS NULL) ORDER BY id DESC LIMIT %s""",
                (CFG["title_model"]["batch"],))
            for r in rows:
                try:
                    v = await asyncio.to_thread(self.titles, r["content"] or "")
                except Exception as e:
                    log.warning("post screening failed: %s", e)
                    if not self.titles_warned:
                        self.titles_warned = True
                        await self.event(f"post screening failed: {e}"[:300], "warn")
                    return 0
                await self.conn.execute(
                    """UPDATE linkedin.job_posts SET ai_offer = %(offer)s, ai_fit = %(fit)s, ai_reason = %(reason)s,
                           ai_checked_at = now(), ai_model = %(model)s,
                           ai_family = %(family)s, ai_sector = %(sector)s, ai_seniority = %(seniority)s,
                           ai_sal_min = %(sal_min)s, ai_sal_max = %(sal_max)s, ai_sal_currency = %(sal_currency)s,
                           ai_sal_period = %(sal_period)s, ai_sal_text = %(sal_text)s,
                           ai_est_min_usd = %(est_min)s, ai_est_max_usd = %(est_max)s,
                           ai_title = CASE WHEN job_title IS NULL AND ai_title IS NULL THEN %(title)s ELSE ai_title END
                       WHERE id = %(id)s""", {**v, "id": r["id"]})
            return len(rows)

    async def fill_matches(self) -> int:
        # Score each job offer against the CV (the read LinkedIn job if any, else the post itself);
        # a changed CV (newer updated_at) rescores them all.
        async with self.screen_lock:
            cv = await self.conn.fetchone("SELECT cv_text, updated_at FROM linkedin.cv_profile WHERE id = 1")
            if not cv:
                return 0
            rows = await self.conn.fetchall(
                """SELECT id, content, job_description FROM linkedin.job_posts
                   WHERE (job_description IS NOT NULL OR ai_offer) AND (ai_match_at IS NULL OR ai_match_at < %s)
                   ORDER BY id DESC LIMIT %s""", (cv["updated_at"], CFG["title_model"]["batch"]))
            for r in rows:
                job = (f"{r['job_description']}\n\nThe LinkedIn post that linked this job:\n{r['content'] or ''}"
                       if r["job_description"] else f"Hiring post (no full job description available):\n{r['content'] or ''}")
                try:
                    v = await asyncio.to_thread(self.matcher, cv["cv_text"], job)
                except Exception as e:
                    log.warning("CV match scoring failed: %s", e)
                    return 0
                await self.conn.execute(
                    "UPDATE linkedin.job_posts SET ai_match_score = %s, ai_match_reason = %s, ai_match_at = now() WHERE id = %s",
                    (v["score"], v["reason"], r["id"]))
            return len(rows)

    async def backfill_screening(self):
        done = 0
        while (n := await self.fill_titles()):
            done += n
        if done:
            await self.event(f"screened {done} older posts with Haiku (job offer / fits criteria)")
        done = 0
        while (n := await self.fill_matches()):
            done += n
        if done:
            await self.event(f"scored {done} LinkedIn jobs against the CV")

    async def telegram_loop(self):
        newest: dict[str, int] = {}
        while (tg := CFG.get("telegram") or {}).get("channels"):
            if self.desired != "running":
                await asyncio.sleep(30)
                continue
            roles = re.compile("|".join(CFG["feed_browse"]["roles"] + [tg["arabic_roles"]]), re.I)
            cutoff = datetime.now(timezone.utc) - timedelta(days=tg["max_age_days"])
            notes = []
            for ch in tg["channels"]:
                try:
                    seen, keep, added = await self.scan_channel(ch, newest, roles, cutoff, tg["first_pages"])
                except Exception as e:
                    log.warning("telegram @%s failed: %s", ch, e)
                    notes.append(f"@{ch} failed ({str(e)[:80]})")
                    continue
                notes.append(f"@{ch} {seen} msgs/{keep} matching/{added} new")
            while await self.fill_titles():
                pass
            while await self.fill_matches():
                pass
            await self.event("Telegram: " + ", ".join(notes), "warn" if any("failed" in n for n in notes) else "info")
            await asyncio.sleep(tg["every_minutes"] * 60)

    async def scan_channel(self, ch: str, newest: dict, roles: re.Pattern, cutoff: datetime, pages: int):
        msgs, before = [], None
        for _ in range(pages if ch not in newest else 3):
            title, page = await asyncio.to_thread(telegram.fetch, ch, before)
            msgs += page
            if not page or min(m["id"] for m in page) <= newest.get(ch, 0) or min(m["at"] for m in page) < cutoff:
                break
            before = min(m["id"] for m in page)
            await asyncio.sleep(random.uniform(1, 3))
        msgs = [m for m in msgs if m["id"] > newest.get(ch, 0) and m["at"] >= cutoff]
        if msgs:
            newest[ch] = max(m["id"] for m in msgs)
        posts = [telegram.to_post(ch, title, m) for m in msgs]
        keep = [p for p in posts if p.is_hiring and roles.search(p.content)]
        added = 0
        for p in keep:
            added += await self.upsert(p, f"Telegram @{ch}")
        return len(msgs), len(keep), added

    @staticmethod
    def feed_due() -> datetime:
        return datetime.now(timezone.utc) + timedelta(minutes=random.uniform(*CFG["feed_browse"]["every_minutes"]))

    async def event(self, message: str, level: str = "info"):
        getattr(log, "warning" if level != "info" else "info")(message)
        await self.conn.execute("INSERT INTO linkedin.scraper_events (level, message) VALUES (%s, %s)", (level, message))

    async def status(self, **fields):
        cols = ", ".join(f"{k} = %s" for k in fields)
        cols = f"{cols}, agent_heartbeat_at = now()" if cols else "agent_heartbeat_at = now()"
        await self.conn.execute(f"UPDATE linkedin.scraper_state SET {cols} WHERE id = 1 AND agent_host = %s",
                                [*fields.values(), HOST])

    async def claim(self, ip: str | None = None) -> bool:
        # Only one agent may drive LinkedIn; another host's fresh heartbeat means it holds the lease.
        row = await self.conn.fetchone(
            f"""UPDATE linkedin.scraper_state SET agent_ip = CASE WHEN agent_host = %s THEN COALESCE(%s, agent_ip) ELSE %s END,
                       agent_host = %s, agent_heartbeat_at = now()
                WHERE id = 1 AND (agent_host IS NULL OR agent_host = %s OR agent_heartbeat_at IS NULL
                                  OR agent_heartbeat_at < now() - interval '{LEASE_SECONDS} seconds')
                RETURNING id""", (HOST, ip, ip, HOST, HOST))
        return row is not None

    async def read_state(self) -> dict:
        row = await self.conn.fetchone("SELECT * FROM linkedin.scraper_state WHERE id = 1")
        self.desired = row["desired_state"]
        return row

    async def heartbeat_loop(self):
        while True:
            await asyncio.sleep(15)
            if not await self.claim():
                self.lease_lost = True
                log.error("lease lost to another agent")
                return
            await self.read_state()

    async def searches_today(self, state: dict) -> int:
        today = datetime.now(TZ).date()
        if state["searches_day"] != today:
            await self.status(searches_today=0, searches_day=today)
            return 0
        return state["searches_today"]

    async def pausable_sleep(self, seconds: float, step: str):
        end = datetime.now(timezone.utc) + timedelta(seconds=seconds)
        while self.desired == "running" and datetime.now(timezone.utc) < end:
            left = int((end - datetime.now(timezone.utc)).total_seconds())
            await self.status(agent_state="pausing", current_step=f"{step} - next search in {left}s")
            await asyncio.sleep(min(5, max(left, 0)))

    async def upsert(self, post, query: str) -> bool:
        d = asdict(post)
        row = await self.conn.fetchone(
            """INSERT INTO linkedin.job_posts (content_hash, post_url, author_name, author_headline, author_profile_url,
                   posted_ago, posted_at_est, content, job_title, company, location, job_url, contact_emails,
                   search_keywords, is_hiring)
               VALUES (%(content_hash)s, %(post_url)s, %(author_name)s, %(author_headline)s, %(author_profile_url)s,
                   %(posted_ago)s, %(posted_at_est)s, %(content)s, %(job_title)s, %(company)s, %(location)s, %(job_url)s,
                   %(contact_emails)s, %(q)s, %(is_hiring)s)
               ON CONFLICT (content_hash) DO UPDATE SET last_seen_at = now(),
                   post_url = COALESCE(linkedin.job_posts.post_url, EXCLUDED.post_url),
                   job_url = COALESCE(linkedin.job_posts.job_url, EXCLUDED.job_url)
               RETURNING (xmax = 0) AS inserted""", {**d, "q": query})
        if row["inserted"] and post.is_hiring and (post.author_profile_url or "").startswith("https://www.linkedin.com/in/"):
            await self.promote_frequent(post)
        return row["inserted"]

    async def promote_frequent(self, post):
        # Authors with 3+ hiring posts join the recruiters list, so their profiles get revisited.
        row = await self.conn.fetchone(
            """INSERT INTO linkedin.recruiters (profile_url, name, headline, found_by)
               SELECT %(url)s, %(name)s, %(headline)s, 'frequent poster'
               WHERE (SELECT count(*) FROM linkedin.job_posts WHERE author_profile_url = %(url)s AND is_hiring) >= 3
               ON CONFLICT (profile_url) DO NOTHING RETURNING 1 AS ok""",
            {"url": post.author_profile_url, "name": post.author_name, "headline": post.author_headline})
        if row:
            await self.event(f"{post.author_name or post.author_profile_url} has 3+ hiring posts; added to recruiters")

    async def search(self, session: ClientSession, query: str) -> dict:
        tool = "search_posts"
        assert tool in ALLOWED_TOOLS
        result = await session.call_tool(tool, {"keywords": query, "date_posted": CFG["date_posted"], "max_pages": CFG["max_pages"]})
        text = "\n".join(c.text for c in result.content if getattr(c, "type", "") == "text")
        if result.is_error:
            raise Blocked(f"search_posts returned an error: {text[:300]}")
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            raise Blocked(f"unexpected response: {text[:300]}")
        url = data.get("url", "")
        if "/search/results/" not in url:
            raise Blocked(f"LinkedIn redirected to {url} (checkpoint/login?)")
        raw = HERE / "raw"
        raw.mkdir(exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        (raw / f"{stamp}-{re.sub(r'[^a-z0-9]+', '-', query.lower())}.json").write_text(text, encoding="utf-8")
        return data

    async def browse_feed(self, session: ClientSession):
        # Scroll the home feed like a person would; hiring posts for our target roles are kept.
        tool = "get_feed"
        assert tool in ALLOWED_TOOLS
        num = random.randint(*CFG["feed_browse"]["posts"])
        await self.status(agent_state="browsing", current_query=None, current_step=f"scrolling the feed (~{num} posts)")
        result = await session.call_tool(tool, {"num_posts": num})
        text = "\n".join(c.text for c in result.content if getattr(c, "type", "") == "text")
        if result.is_error:
            raise Blocked(f"get_feed returned an error: {text[:300]}")
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            raise Blocked(f"unexpected feed response: {text[:300]}")
        if "/feed" not in data.get("url", "") or data.get("section_errors"):
            raise Blocked(f"feed problem: {json.dumps(data.get('section_errors') or data.get('url'))[:300]}")
        raw = HERE / "raw"
        raw.mkdir(exist_ok=True)
        (raw / f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-home-feed.json").write_text(text, encoding="utf-8")
        roles = role_pattern()
        posts = parse_search_results(data, section="feed")
        keep = [p for p in posts if p.is_hiring and roles.search(f"{p.job_title or ''} {p.content}")]
        added = 0
        for p in keep:
            added += await self.upsert(p, FEED_LABEL)
        await self.fill_titles()
        await self.fill_matches()
        await self.event(f"browsed the feed: {len(posts)} posts, {len(keep)} matching, {added} new")

    async def pick_profile(self) -> str | None:
        # A random author of a matching hiring post, or a recruiter found by people search,
        # not visited recently, within the daily cap.
        pv = CFG["profile_visits"]
        args = {"days": pv["revisit_days"], "tz": CFG["timezone"], "cap": pv["max_per_day"]}
        fresh = """NOT EXISTS (SELECT 1 FROM linkedin.profile_visits v WHERE v.profile_url = {col}
                                 AND v.visited_at > now() - make_interval(days => %(days)s))
                 AND (SELECT count(*) FROM linkedin.profile_visits
                      WHERE visited_at >= date_trunc('day', now() AT TIME ZONE %(tz)s) AT TIME ZONE %(tz)s) < %(cap)s"""
        rows = await self.conn.fetchall(
            f"""SELECT p.author_profile_url AS url, p.job_title, p.content, p.author_headline FROM linkedin.job_posts p
               WHERE p.is_hiring AND p.author_profile_url LIKE 'https://www.linkedin.com/in/%%'
                 AND {fresh.format(col='p.author_profile_url')}""", args)
        roles, region = role_pattern(), re.compile(CFG["recruiter_search"]["locations"], re.I)
        urls = {r["url"] for r in rows if roles.search(f"{r['job_title'] or ''} {r['content']}")
                and region.search(f"{r['content']} {r['author_headline'] or ''}")}
        rows = await self.conn.fetchall(
            f"SELECT r.profile_url AS url FROM linkedin.recruiters r WHERE {fresh.format(col='r.profile_url')}", args)
        urls |= {r["url"] for r in rows}
        # The LinkedIn tool only accepts letters, digits, _ and - (no emoji or symbols such as ®).
        urls = {u for u in urls if re.fullmatch(r"[\w-]+", profile_username(u))}
        return random.choice(sorted(urls)) if urls else None

    async def find_recruiters(self, session: ClientSession) -> bool:
        # People search for tech recruiters in our region; counts toward the daily search cap.
        tool = "search_people"
        assert tool in ALLOWED_TOOLS
        rs = CFG["recruiter_search"]
        done = await self.searches_today(await self.read_state())
        row = await self.conn.fetchone(
            """SELECT count(*) AS n FROM linkedin.recruiter_searches
               WHERE searched_at >= date_trunc('day', now() AT TIME ZONE %(tz)s) AT TIME ZONE %(tz)s""",
            {"tz": CFG["timezone"]})
        if done >= CFG["max_searches_per_day"] or row["n"] >= rs["max_per_day"]:
            return False
        query = random.choice(rs["queries"])
        await self.status(agent_state="browsing", current_query=None, current_step=f'looking for recruiters: "{query}"',
                          searches_today=done + 1, searches_day=datetime.now(TZ).date())
        result = await session.call_tool(tool, {"keywords": query})
        text = "\n".join(c.text for c in result.content if getattr(c, "type", "") == "text")
        if result.is_error:
            raise Blocked(f"search_people returned an error: {text[:300]}")
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            raise Blocked(f"unexpected people search response: {text[:300]}")
        if "/search/results/" not in data.get("url", ""):
            raise Blocked(f"LinkedIn redirected to {data.get('url')} (checkpoint/login?)")
        raw = HERE / "raw"
        raw.mkdir(exist_ok=True)
        (raw / f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-people-{re.sub(r'[^a-z0-9]+', '-', query.lower())}.json").write_text(text, encoding="utf-8")
        people = parse_people(data)
        headline, region = re.compile(rs["headline"], re.I), re.compile(rs["locations"], re.I)
        keep = [p for p in people if headline.search(p["headline"]) and region.search(f"{p['location']} {p['headline']}")]
        added = 0
        for p in keep:
            row = await self.conn.fetchone(
                """INSERT INTO linkedin.recruiters (profile_url, name, headline, location, found_by)
                   VALUES (%s, %s, %s, %s, %s) ON CONFLICT (profile_url) DO NOTHING RETURNING 1 AS ok""",
                (p["url"], p["name"], p["headline"], p["location"], query))
            added += row is not None
        await self.conn.execute("INSERT INTO linkedin.recruiter_searches (query, results, kept, new) VALUES (%s, %s, %s, %s)",
                                (query, len(people), len(keep), added))
        await self.event(f'looked for recruiters "{query}": {len(people)} people, {len(keep)} tech recruiters in region, {added} new')
        return True

    async def visit_profile(self, session: ClientSession, url: str):
        tool = "get_person_profile"
        assert tool in ALLOWED_TOOLS
        pv = CFG["profile_visits"]
        username = profile_username(url)
        await self.status(agent_state="browsing", current_query=None, current_step=f"reading recent posts of {username}")
        result = await session.call_tool(tool, {"linkedin_username": username, "sections": "posts",
                                                "max_scrolls": random.randint(*pv["max_scrolls"])})
        text = "\n".join(c.text for c in result.content if getattr(c, "type", "") == "text")
        if result.is_error:
            raise Blocked(f"get_person_profile returned an error: {text[:300]}")
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            raise Blocked(f"unexpected profile response: {text[:300]}")
        if "/in/" not in data.get("url", ""):
            raise Blocked(f"LinkedIn redirected to {data.get('url')} (checkpoint/login?)")
        raw = HERE / "raw"
        raw.mkdir(exist_ok=True)
        (raw / f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-profile-{re.sub(r'[^a-z0-9]+', '-', username.lower())}.json").write_text(text, encoding="utf-8")
        roles = role_pattern()
        cutoff = datetime.now(timezone.utc) - timedelta(days=pv["max_age_days"])
        posts = parse_search_results(data, section="posts")
        keep = [p for p in posts if p.is_hiring and p.posted_at_est and p.posted_at_est >= cutoff
                and roles.search(f"{p.job_title or ''} {p.content}")]
        added = 0
        for p in keep:
            added += await self.upsert(p, PROFILE_LABEL)
        await self.conn.execute(
            """INSERT INTO linkedin.profile_visits (profile_url, posts_seen, posts_matching, posts_new)
               VALUES (%s, %s, %s, %s)""", (url, len(posts), len(keep), added))
        await self.fill_titles()
        await self.fill_matches()
        note = f"; {data['section_errors']}"[:200] if data.get("section_errors") else ""
        await self.event(f"read recent posts of {username}: {len(posts)} posts, {len(keep)} matching, {added} new{note}")

    async def pick_job(self) -> dict | None:
        # Newest post without an email that links a LinkedIn job we haven't read, within the daily cap.
        return await self.conn.fetchone(
            """SELECT id, substring(job_url from '/jobs/view/(\\d+)') AS job_id FROM linkedin.job_posts
               WHERE is_hiring AND job_fetched_at IS NULL AND job_url ~ '/jobs/view/\\d+'
                 AND cardinality(coalesce(contact_emails, '{}')) = 0 AND ai_fit IS NOT FALSE
                 AND (SELECT count(*) FROM linkedin.job_posts
                      WHERE job_fetched_at >= date_trunc('day', now() AT TIME ZONE %(tz)s) AT TIME ZONE %(tz)s) < %(cap)s
               ORDER BY id DESC LIMIT 1""", {"tz": CFG["timezone"], "cap": CFG["job_details"]["max_per_day"]})

    async def read_job(self, session: ClientSession, job: dict):
        tool = "get_job_details"
        assert tool in ALLOWED_TOOLS
        await self.status(agent_state="browsing", current_query=None, current_step=f"reading LinkedIn job {job['job_id']}")
        result = await session.call_tool(tool, {"job_id": job["job_id"]})
        text = "\n".join(c.text for c in result.content if getattr(c, "type", "") == "text")
        if result.is_error:
            raise Blocked(f"get_job_details returned an error: {text[:300]}")
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            raise Blocked(f"unexpected job response: {text[:300]}")
        url = data.get("url", "")
        if re.search(r"/(login|checkpoint|authwall|uas/)", url):
            raise Blocked(f"LinkedIn redirected to {url} (checkpoint/login?)")
        raw = HERE / "raw"
        raw.mkdir(exist_ok=True)
        (raw / f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-job-{job['job_id']}.json").write_text(text, encoding="utf-8")
        desc = (data.get("sections") or {}).get("job_posting")
        err = json.dumps(data["section_errors"])[:500] if data.get("section_errors") else None
        if not desc and "/jobs/view/" not in url:
            err = f"redirected to {url}"[:500]
        await self.conn.execute(
            """UPDATE linkedin.job_posts SET job_description = %s, job_fetched_at = now(), job_fetch_error = %s,
                      ai_match_at = CASE WHEN %s THEN NULL ELSE ai_match_at END
               WHERE id = %s OR (job_fetched_at IS NULL AND substring(job_url from '/jobs/view/(\\d+)') = %s)""",
            (desc[:20000] if desc else None, err, bool(desc), job["id"], job["job_id"]))
        await self.event(f"read LinkedIn job {job['job_id']} for post #{job['id']}: "
                         + (f"{len(desc)} chars" if desc else f"no description ({err or 'empty'})")[:250])
        await self.fill_matches()

    async def side_trip(self, session: ClientSession, gap, step: str):
        # Now and then between searches: scroll the feed, read a recruiter's recent posts, look for recruiters,
        # or read a LinkedIn job a post links to.
        r = random.random()
        feed = CFG["feed_browse"]["between_searches_chance"]
        visit = feed + CFG["profile_visits"]["between_searches_chance"]
        people = visit + CFG["recruiter_search"]["between_searches_chance"]
        if r < feed:
            trip = lambda: self.browse_feed(session)
        elif r < visit and (url := await self.pick_profile()):
            trip = lambda: self.visit_profile(session, url)
        elif visit <= r < people:
            trip = lambda: self.find_recruiters(session)
        elif people <= r < people + CFG["job_details"]["between_searches_chance"] and (job := await self.pick_job()):
            trip = lambda: self.read_job(session, job)
        else:
            return
        await self.pausable_sleep(random.uniform(*gap) / 2, step)
        if self.desired == "running":
            await trip()

    async def idle_trip(self):
        # Between cycles: read a linked LinkedIn job or a recruiter's recent posts (no search cost),
        # look for recruiters, or scroll the feed.
        params = StdioServerParameters(command=CFG["mcp_server"][0], args=CFG["mcp_server"][1:])
        job = await self.pick_job() if random.random() < CFG["job_details"]["idle_chance"] else None
        url = await self.pick_profile() if not job and random.random() < 0.6 else None
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            if job:
                await self.read_job(session, job)
            elif url:
                await self.visit_profile(session, url)
            elif not (random.random() < 0.3 and await self.find_recruiters(session)):
                await self.browse_feed(session)
        await self.status(agent_state="waiting", current_step=None)

    async def run_cycle(self, state: dict):
        CFG.update(load_cfg())
        await self.status(search_config=Jsonb({k: CFG[k] for k in SEARCH_CONFIG_KEYS}))
        test = state["run_until"] is not None
        exception = bool(state.get("run_now")) and not test
        gap = CFG["test_gap_seconds"] if test else CFG["gap_seconds"]
        queries = random.sample(CFG["queries"], min(CFG.get("searches_per_cycle", len(CFG["queries"])), len(CFG["queries"])))
        seen = new = 0
        await self.status(agent_state="starting", current_step="launching browser", current_query=None, next_cycle_at=None,
                          cycle_started_at=datetime.now(timezone.utc), cycle_posts_seen=0, cycle_posts_new=0, last_error=None,
                          run_now=False)
        await self.event(f"{'run now: ' if exception else ''}cycle started ({len(queries)} searches)")
        params = StdioServerParameters(command=CFG["mcp_server"][0], args=CFG["mcp_server"][1:])
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            for i, query in enumerate(queries, 1):
                if self.lease_lost:
                    raise LeaseLost("another agent took over")
                state = await self.read_state()
                if self.desired != "running":
                    await self.event("stopped by user")
                    break
                if test and (datetime.now(timezone.utc) >= state["run_until"] or seen >= state["run_max_posts"]):
                    break
                done = await self.searches_today(state)
                if not exception and done >= CFG["max_searches_per_day"]:
                    await self.event(f"daily cap of {CFG['max_searches_per_day']} searches reached", "warn")
                    break
                step = f"search {i}/{len(queries)}"
                await self.status(agent_state="searching", current_query=query, current_step=step,
                                  searches_today=done + 1, searches_day=datetime.now(TZ).date())
                posts = parse_search_results(await self.search(session, query))
                roles = role_pattern()
                keep = [p for p in posts if roles.search(f"{p.job_title or ''} {p.content}")]
                added = 0
                for p in keep:
                    added += await self.upsert(p, query)
                await self.fill_titles()
                await self.fill_matches()
                seen, new = seen + len(posts), new + added
                await self.status(cycle_posts_seen=seen, cycle_posts_new=new)
                await self.event(f'"{query}": {len(posts)} posts, {len(posts) - len(keep)} off-target, {added} new')
                if i < len(queries):
                    if not test:
                        await self.side_trip(session, gap, step)
                    await self.pausable_sleep(random.uniform(*gap), step)
        if test:
            await self.conn.execute("""UPDATE linkedin.scraper_state SET desired_state = 'stopped', desired_by = 'agent (test run done)',
                                       desired_at = now(), run_until = NULL, run_max_posts = NULL, next_cycle_at = NULL WHERE id = 1""")
            await self.status(agent_state="stopped", current_query=None, current_step=None)
            await self.event(f"test run finished: {seen} posts seen, {new} new")
            return
        if self.desired != "running":
            await self.status(agent_state="stopped", current_query=None, current_step=None)
            return
        jitter = random.uniform(-CFG["cycle_jitter_minutes"], CFG["cycle_jitter_minutes"])
        next_at = datetime.now(timezone.utc) + timedelta(hours=CFG["cycle_every_hours"], minutes=jitter)
        await self.status(agent_state="waiting", current_query=None, current_step=None, next_cycle_at=next_at)
        await self.event(f"cycle finished: {seen} posts seen, {new} new")

    async def run(self):
        info = await asyncio.to_thread(ip_info)
        ip = info["ip"]
        while not await self.claim(ip):
            row = await self.read_state()
            log.warning("standby: agent %s (%s) is active; waiting for it to stop", row["agent_host"], row.get("agent_ip"))
            await asyncio.sleep(30)
        await self.status(linkedin_account=CFG["linkedin_account"],
                          search_config=Jsonb({k: CFG[k] for k in SEARCH_CONFIG_KEYS}),
                          agent_ip_country=info["country"], agent_ip_city=info["city"], agent_ip_org=info["org"])
        where = ", ".join(filter(None, [info["city"], info["country"], info["org"]]))
        await self.event(f"agent online on {HOST} ({ip or 'unknown IP'}{'; ' + where if where else ''}) as LinkedIn {CFG['linkedin_account']}")
        hb = asyncio.create_task(self.heartbeat_loop())
        backfill = asyncio.create_task(self.backfill_screening())
        tg = asyncio.create_task(self.telegram_loop())
        try:
            while True:
                if self.lease_lost:
                    raise LeaseLost("another agent took over")
                state = await self.read_state()
                now = datetime.now(timezone.utc)
                local_hour = datetime.now(TZ).hour
                start_h, end_h = CFG["active_hours"]
                if state["agent_state"] in ("blocked", "error") and self.desired == "running":
                    await self.status(agent_state="waiting", last_error=None)
                    await self.event("restarted by user after a stop", "warn")
                    state["agent_state"] = "waiting"
                if self.desired != "running":
                    if state["agent_state"] not in ("blocked", "error"):
                        await self.status(agent_state="stopped", current_step=None)
                elif state.get("run_now") and state["run_until"] is None:
                    await self.guarded(self.run_cycle(state))
                elif state["run_until"] is None and not (start_h <= local_hour < end_h):
                    local = datetime.now(TZ)
                    opens = local.replace(hour=start_h, minute=0, second=0, microsecond=0)
                    if local_hour >= end_h:
                        opens += timedelta(days=1)
                    opens_cairo = opens.astimezone(CAIRO).strftime("%b %d, %I:%M %p")
                    await self.status(agent_state="waiting", next_cycle_at=opens,
                                      current_step=f"outside active hours {start_h}:00-{end_h}:00 {CFG['timezone']}; starts {opens_cairo} Cairo")
                elif state["run_until"] is None and (state["next_cycle_at"] and now < state["next_cycle_at"]
                                                     or await self.searches_today(state) >= CFG["max_searches_per_day"]):
                    if now >= self.next_feed_at:
                        self.next_feed_at = self.feed_due()
                        await self.guarded(self.idle_trip())
                    else:
                        capped = await self.searches_today(state) >= CFG["max_searches_per_day"]
                        await self.status(agent_state="waiting",
                                          current_step="daily search cap reached" if capped else "next cycle scheduled")
                elif await self.searches_today(state) >= CFG["max_searches_per_day"]:
                    await self.status(agent_state="waiting", current_step="daily search cap reached")
                else:
                    await self.guarded(self.run_cycle(state))
                await asyncio.sleep(15)
        finally:
            hb.cancel()
            backfill.cancel()
            tg.cancel()

    async def retry_later(self, step: str, error: str, message: str):
        retry_at = datetime.now(timezone.utc) + timedelta(minutes=10)
        await self.status(agent_state="waiting", current_query=None, current_step=step,
                          next_cycle_at=retry_at, last_error=error[:1000])
        await self.event(message, "warn")

    async def guarded(self, coro):
        if not await asyncio.to_thread(online):
            coro.close()
            await self.status(agent_state="waiting", current_query=None, current_step="no internet; waiting")
            if not self.offline:
                self.offline = True
                await self.event("no internet (Mac asleep?); waiting for the connection", "warn")
            return
        self.offline = False
        try:
            await coro
        except (psycopg.Error, LeaseLost):
            raise
        except Exception as e:
            # The MCP client's task group wraps errors raised inside it.
            while isinstance(e, BaseExceptionGroup) and len(e.exceptions) == 1:
                e = e.exceptions[0]
            if "Connection closed" in str(e):
                # The browser died, usually because the Mac slept mid-cycle; retry rather than stop.
                await self.retry_later("browser connection lost; retrying", str(e), "browser connection lost (Mac asleep?); retrying in 10 min")
                return
            # The tool treats any failed page load as a logout and sets the still-valid session aside.
            if "Session expired" in str(e) and time.time() - self.restored_at > 1800 \
                    and (backup := await asyncio.to_thread(network_dropped_session)):
                await asyncio.to_thread(restore_session, backup)
                self.restored_at = time.time()
                await self.retry_later("LinkedIn session restored; retrying", str(e),
                                       f"network failure made the LinkedIn tool drop the session; restored {backup.name}, retrying in 10 min")
                return
            # The connection can drop after the online() check passed; that's not LinkedIn blocking us.
            if "net::ERR_" in str(e) or not await asyncio.to_thread(online):
                await self.retry_later("network error; retrying", str(e), "network dropped mid-run (Mac asleep or Wi-Fi?); retrying in 10 min")
                return
            kind = "blocked" if isinstance(e, Blocked) else "error"
            await self.conn.execute("UPDATE linkedin.scraper_state SET desired_state = 'stopped', desired_by = 'agent', desired_at = now() WHERE id = 1")
            await self.status(agent_state=kind, current_query=None, current_step=None, last_error=str(e)[:1000])
            await self.event(f"STOPPED ({kind}): {e}", "error")


async def main():
    while True:
        tunnel = None
        try:
            tunnel = start_tunnel()
            with psycopg.Connection.connect(
                    host="127.0.0.1", port=CFG["ssh"]["local_port"], dbname=CFG["db"]["dbname"], user=CFG["db"]["user"],
                    password=db_password(), autocommit=True, connect_timeout=15, row_factory=dict_row,
                    prepare_threshold=None) as conn:  # deploys ALTER tables; cached plans would then fail
                await Agent(Db(conn)).run()
        except (psycopg.Error, RuntimeError, OSError) as e:
            log.error("connection problem, retrying in 60s: %s", e)
        finally:
            if tunnel:
                tunnel.kill()
        await asyncio.sleep(60)


if __name__ == "__main__":
    asyncio.run(main())

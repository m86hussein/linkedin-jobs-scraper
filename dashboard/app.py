import asyncio
import base64
import binascii
import json
import logging
import os
import re
import smtplib
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel

HERE = Path(__file__).parent
PUBLIC_HOST = os.environ.get("PUBLIC_HOST", "jobs.example.com")
HEARTBEAT_STALE_SECONDS = 90
TEST_RUN_MINUTES = 5
TEST_RUN_MAX_POSTS = 100
# Written by deploy/setup-server.sh
BUILD = json.loads((HERE / "build.json").read_text()) if (HERE / "build.json").exists() else {}

# Gmail: OAuth refresh token (gmail.send) from deploy/gmail-oauth-login.py, else SMTP with an app password.
GMAIL_USER = os.environ.get("GMAIL_USER", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
GMAIL_OAUTH_FILE = Path(os.environ.get("GMAIL_OAUTH_FILE", "/etc/jobsscrap/gmail-oauth.json"))
MAIL_FROM_NAME = os.environ.get("MAIL_FROM_NAME", "")
# The sign-in allowlist is shared with other apps; only these users may send from the mailbox.
MAIL_USERS = {e.strip().lower() for e in os.environ.get("MAIL_ALLOWED_USERS", GMAIL_USER).split(",") if e.strip()}
CV_DIR = Path(os.environ.get("CV_DIR", "/etc/jobsscrap/cv"))
ATTACH_TYPES = {".pdf": "application/pdf", ".doc": "application/msword",
                ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}
MAX_ATTACHMENT = 5 * 1024 * 1024
EMAIL_RE = re.compile(r"[^@\s<>,;\"'()\[\]\\]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")

pool = AsyncConnectionPool(os.environ["DB_DSN"], min_size=1, max_size=4, open=False,
                           kwargs={"autocommit": True, "row_factory": dict_row})
log = logging.getLogger("uvicorn.error")  # uvicorn's configured logger -> journald


@asynccontextmanager
async def lifespan(_: FastAPI):
    await pool.open()
    await sync_cv_text()
    yield
    await pool.close()


def cv_text(cv: Path) -> str | None:
    if cv.suffix.lower() != ".pdf":
        return None
    from pypdf import PdfReader
    return "\n".join(p.extract_text() or "" for p in PdfReader(cv).pages).strip() or None


async def sync_cv_text():
    # The agent scores LinkedIn jobs against this text; it rescores only when the text changes.
    cv = default_cv()
    try:
        text = cv and await asyncio.to_thread(cv_text, cv)
    except Exception as e:
        log.warning("could not read the CV text from %s: %s", cv, e)
        return
    if not text:
        return
    async with pool.connection() as conn:
        await conn.execute(
            """INSERT INTO linkedin.cv_profile (id, filename, cv_text) VALUES (1, %s, %s)
               ON CONFLICT (id) DO UPDATE SET filename = EXCLUDED.filename, cv_text = EXCLUDED.cv_text, updated_at = now()
               WHERE linkedin.cv_profile.cv_text IS DISTINCT FROM EXCLUDED.cv_text""", (cv.name, text[:30000]))


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/flags", StaticFiles(directory=HERE / "static" / "flags"), name="flags")

# Order matters: first match wins, location is checked before the post text.
COUNTRIES = [
    ("sa", r"saudi|ksa|riyadh|jeddah|dammam|khobar|makkah|mecca|madinah|medina|neom|dhahran|jubail"),
    ("ae", r"uae|united arab emirates|emirates|dubai|abu dhabi|sharjah"),
    ("qa", r"qatar|doha"),
    ("kw", r"kuwait"),
    ("bh", r"bahrain|manama"),
    ("om", r"oman|muscat"),
    ("eg", r"egypt|cairo|alexandria|giza|heliopolis|maadi|nasr city|new capital|smart village|sheikh zayed|مصر|القاهرة|الإسكندرية|الجيزة"),
    ("jo", r"jordan|amman"),
    ("lb", r"lebanon|beirut"),
    ("ma", r"morocco|casablanca"),
    ("tr", r"turkey|türkiye|istanbul"),
    ("pk", r"pakistan|karachi|lahore|islamabad"),
    ("in", r"india|bengaluru|bangalore|hyderabad|pune|chennai|mumbai|delhi|noida|gurugram|gurgaon"),
    ("gb", r"united kingdom|uk|london|manchester|edinburgh"),
    ("ie", r"ireland|dublin"),
    ("ca", r"canada|toronto|vancouver|montreal"),
    ("us", r"united states|usa|u\.s\.|us-based|us based|north america|americas|new york|san francisco|seattle|chicago|austin|boston"),
    ("de", r"germany|berlin|munich|frankfurt"),
    ("fr", r"france|paris"),
    ("nl", r"netherlands|amsterdam"),
    ("lv", r"latvia|riga"),
    ("pl", r"poland|warsaw|krakow"),
    ("sg", r"singapore"),
    ("my", r"malaysia|kuala lumpur"),
    ("au", r"australia|sydney|melbourne"),
    # Region-wide, only when no specific country matched.
    ("mena", r"mena|middle east|gcc|gulf region|arabian gulf"),
]
COUNTRY_RES = [(code, re.compile(rf"\b(?:{pat})\b", re.I)) for code, pat in COUNTRIES]
REMOTE_RE = re.compile(r"\bremote\b", re.I)
# LinkedIn job locations look like "Issaquah, WA (Hybrid)"; only trusted in the location field.
US_STATE_RE = re.compile(r",\s*(?:AL|AZ|CA|CO|CT|FL|GA|IL|MA|MD|MI|MN|NC|NJ|NY|OH|OR|PA|TX|VA|WA)\b")
WORK_MODE_RE = re.compile(r"\((?:remote|hybrid|on-?site)\)", re.I)
PLACELESS_RE = re.compile(r"remote|worldwide|anywhere|global", re.I)
# Telegram channels that serve one market: their posts often name no place at all.
CHANNEL_COUNTRY = {"itcjobs": "sa", "technomajed": "sa", "jobsbarq": "sa", "OPENVACANCIES2022": "eg",
                   "remotejobss": "remote", "remote_ai_jobs": "remote", "remotejobs": "remote", "remotetechjobss": "remote"}
REMOTE_MODE_RE = re.compile(r"\bremote(?:ly)?\b|\bwork(?:ing)?\s+from\s+home\b|\bwfh\b|عن\s*بعد|عن\s*بُعد|من\s+المنزل", re.I)
NOT_REMOTE_RE = re.compile(r"\b(?:no|not|non)[- ]remote\b", re.I)
HYBRID_RE = re.compile(r"\bhybrid\b(?!\s+(?:cloud|apps?|mobile|infrastructure|environments?|architecture|solutions?|IT)\b)|هجين", re.I)


def work_mode_of(location: str | None, content: str | None) -> str:
    """onsite, hybrid or remote; a post that mentions neither remote nor hybrid is taken as on-site."""
    if m := WORK_MODE_RE.search(location or ""):
        mode = m.group(0).strip("()").lower()
        return "onsite" if "site" in mode else mode
    text = f"{location or ''}\n{content or ''}"
    if HYBRID_RE.search(text):
        return "hybrid"
    return "remote" if REMOTE_MODE_RE.search(text) and not NOT_REMOTE_RE.search(text) else "onsite"


def country_of(location: str | None, content: str | None) -> str | None:
    if location and US_STATE_RE.search(location):
        return "us"
    for code, rx in COUNTRY_RES:
        if location and rx.search(location):
            return code
    # A named place we don't recognise (e.g. "Dallas-Fort Worth Metroplex (Remote)") is somewhere else.
    place = WORK_MODE_RE.sub("", location or "").strip(" ,-")
    if place and not PLACELESS_RE.fullmatch(place):
        return "other"
    for code, rx in COUNTRY_RES:
        if content and rx.search(content):
            return code
    return "remote" if REMOTE_RE.search(f"{location or ''} {content or ''}") else None


def post_country(location: str | None, content: str | None, search_keywords: str | None) -> str | None:
    channel = CHANNEL_COUNTRY.get((search_keywords or "").removeprefix("Telegram @"))
    country = country_of(location, content)
    # An Egypt channel's placeless remote post is an Egypt remote role.
    if not country or (country == "remote" and channel == "eg"):
        country = channel
    return country


CURRENCIES = [
    ("USD", r"US\$|USD|\$|US dollars?|dollars?"),
    ("EGP", r"EGP|(?-i:LE|L\.E\.?)|E£|egyptian pounds?|جنيه(?:ا|ات)?"),
    ("SAR", r"SAR|(?-i:SR)|﷼|saudi riyals?|riyals?|ريال(?:ا|ات)?"),
]
CUR_RE = "|".join(f"(?:{p})" for _, p in CURRENCIES)
CUR_OF = [(code, re.compile(rf"^(?:{p})$", re.I)) for code, p in CURRENCIES]
NUM = r"\d{1,3}(?:[,\s]\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?"
AMOUNT = rf"(?:{NUM})(?:[kK]\b|\s?(?:thousand|ألف|الف))?"
# "$5M funding", "$1.5B platform", "10+ years", "20% bonus" are not salaries.
NOT_SALARY = r"(?!\s*(?:m\b|mn\b|million|b\b|bn\b|billion|%|\+?\s*(?:years?|yrs)\b))"
RANGE_SEP = r"\s*(?:-|–|—|~|to|and|إلى|الى)\s*"
_cur = rf"(?P<cur>{CUR_RE})"
_rng = rf"(?P<a>{AMOUNT}){NOT_SALARY}(?:{RANGE_SEP}(?:(?:{CUR_RE})\s?)?(?P<b>{AMOUNT}){NOT_SALARY})?"
SALARY_RES = [re.compile(rf"(?<![\w$]){_cur}\s?{_rng}(?!\w)", re.I),
              re.compile(rf"(?<![\w.,]){_rng}\s?{_cur}(?!\w)", re.I)]
SALARY_CONTEXT_RE = re.compile(
    r"salar|compensation|\bpay\b|paid|package|remuneration|\bctc\b|\bote\b|\bbase\b|gross|stipend|"
    r"راتب|الراتب|رواتب|مرتب|المرتب", re.I)
PERIODS = [
    ("hour", re.compile(r"per\s+hour|/\s*h(?:ou)?r\b|an\s+hour|hourly|بالساعة", re.I)),
    ("month", re.compile(r"per\s+month|/\s*mo(?:nth)?\b|a\s+month|monthly|p\.m\.|\bmonth\b|شهري|شهريا|بالشهر|في الشهر", re.I)),
    ("year", re.compile(r"per\s+(?:year|annum)|/\s*y(?:ea)?r\b|a\s+year|annual(?:ly)?|yearly|p\.a\.|\byear\b|سنوي|سنويا|بالسنة|في السنة", re.I)),
]
# Unstated period: amounts at or above this are assumed yearly, below monthly.
YEARLY_FROM = {"USD": 20_000, "SAR": 150_000, "EGP": 1_000_000}


def _amount(s: str) -> float:
    s = s.strip()
    mult = 1000 if re.search(r"[kK]$|thousand|ألف|الف", s) else 1
    num = re.match(NUM, s).group(0)
    num = re.sub(r"[,\s](?=\d{3}\b)", "", num)
    return float(num) * mult


def _period(text: str, start: int, end: int) -> str | None:
    for window in (text[end:end + 30], text[max(0, start - 40):start]):
        for name, rx in PERIODS:
            if rx.search(window):
                return name
    return None


def salary_of(text: str | None) -> dict:
    """First salary figure in the post: amount (or range), currency, and pay period if stated."""
    for m in sorted((m for rx in SALARY_RES for m in rx.finditer(text or "")), key=lambda m: m.start()):
        period = _period(text, m.start(), m.end())
        near = text[max(0, m.start() - 100):m.end() + 40]
        if not period and not SALARY_CONTEXT_RE.search(near):
            continue
        lo = _amount(m["a"])
        hi = _amount(m["b"]) if m["b"] else None
        if hi and hi >= 1000 and lo < 1000 and not re.search(r"[kK]|thousand|ألف|الف", m["a"]):
            lo *= 1000  # "SAR 25-30k"
        if lo < (5 if period == "hour" else 300) or (hi is not None and hi < lo):
            continue
        cur = next(code for code, rx in CUR_OF if rx.match(m["cur"].strip()))
        inferred = period is None
        if inferred:
            period = "year" if (hi or lo) >= YEARLY_FROM[cur] else "month"
        return {"salary_min": lo, "salary_max": hi, "salary_currency": cur, "salary_period": period,
                "salary_period_inferred": inferred, "salary_text": " ".join(m.group(0).split())}
    return {"salary_min": None, "salary_max": None, "salary_currency": None, "salary_period": None,
            "salary_period_inferred": False, "salary_text": None}


# Target market: Saudi, Qatar, Oman and Egypt jobs (Egypt on-site or remote), remote/MENA roles not tied to another
# country, and roles anywhere that sponsor a visa.
KSA_REMOTE = {"sa", "qa", "om", "eg", "remote", "mena"}
VISA_RE = re.compile(r"\bvisa\s+sponsor|\bsponsor(?:s|ship|ing)?\s+(?:\w+\s+){0,2}visas?\b", re.I)
NO_VISA_RE = re.compile(r"\b(?:no|not|unable\s+to|cannot|can't|won't|without)\s+(?:\w+\s+){0,3}(?:visa\s+)?sponsor", re.I)


def sponsors_visa(content: str | None) -> bool:
    return bool(VISA_RE.search(content or "")) and not NO_VISA_RE.search(content or "")


def in_target(country: str | None, content: str | None) -> bool:
    return country in KSA_REMOTE or sponsors_visa(content)


# Roles restricted to Saudi nationals; "Saudi Arabia" and "Saudi preferred" don't count.
SAUDI_ONLY_RE = re.compile(
    r"\bsaudi(?:s|\s+nationals?|\s+citizens?|\s+candidates?)?\s*only\b|"
    r"\bonly\s+(?:for\s+|open\s+to\s+)?saudi(?:s|\s+nationals?|\s+citizens?)?\b(?!\s*arabia)|"
    r"\bsaudi\s+(?:nationals?|citizens?)\s+(?:are\s+)?(?:required|needed)\b|"
    r"\bnationality\s*:?\s*saudi\b(?!\s*arabia)|\bsaudi\s+nationality\b|"
    r"\bmust\s+be\s+(?:a\s+)?saudi\b(?!\s*arabia)|\bsaudi[sz]ation\b|"
    r"للسعوديين|للسعوديات|سعوديين\s+فقط|سعوديات\s+فقط|سعودي(?:ة)?\s+فقط|سعودي(?:ة)?\s+الجنسية|الجنسية\s*:?\s*(?:ال)?سعودي|توطين", re.I)


def user(request: Request) -> str:
    # Set by oauth2-proxy; nginx strips any client-supplied value.
    return request.headers.get("x-forwarded-email", "unknown")


@app.get("/")
async def index():
    return FileResponse(HERE / "static" / "index.html")


# Public (oauth2-proxy skip_auth_routes); otherwise sign-out bounces straight back into Google login.
@app.get("/signed-out")
async def signed_out():
    return HTMLResponse("""<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width, initial-scale=1">
<title>Signed out</title><body style="margin:0;display:grid;place-items:center;min-height:100vh;background:#0f1115;color:#e6e9ef;font:15px system-ui,sans-serif">
<div style="text-align:center"><h1 style="font-size:20px">You're signed out</h1>
<a href="/" style="display:inline-block;margin-top:8px;padding:8px 18px;border:1px solid #2a2f3a;border-radius:8px;color:#4c8dff;text-decoration:none">Sign in to LinkedIn Jobs Scraper</a></div>""")


@app.get("/api/me")
async def me(request: Request):
    return {"email": user(request), "build": BUILD}


@app.get("/api/prefs")
async def get_prefs(request: Request):
    async with pool.connection() as conn:
        row = await (await conn.execute("SELECT prefs FROM linkedin.dashboard_prefs WHERE user_email = %s",
                                        (user(request),))).fetchone()
    return row["prefs"] if row else {}


@app.put("/api/prefs")
async def put_prefs(prefs: dict[str, str], request: Request):
    if request.headers.get("origin") not in (None, f"https://{PUBLIC_HOST}"):
        raise HTTPException(403, "bad origin")
    data = json.dumps(prefs)
    if len(data) > 65536:
        raise HTTPException(413, "prefs too large")
    async with pool.connection() as conn:
        await conn.execute(
            """INSERT INTO linkedin.dashboard_prefs (user_email, prefs) VALUES (%s, %s::jsonb)
               ON CONFLICT (user_email) DO UPDATE SET prefs = EXCLUDED.prefs, updated_at = now()""",
            (user(request), data))
    return {"ok": True}


@app.get("/api/status")
async def status():
    async with pool.connection() as conn:
        state = await (await conn.execute("SELECT * FROM linkedin.scraper_state WHERE id = 1")).fetchone()
        counts = await (await conn.execute(
            """SELECT count(*) AS total,
                      count(*) FILTER (WHERE is_hiring) AS hiring,
                      count(*) FILTER (WHERE first_seen_at > now() - interval '24 hours') AS last_24h,
                      count(*) FILTER (WHERE first_seen_at >= date_trunc('day', now() AT TIME ZONE 'Africa/Cairo') AT TIME ZONE 'Africa/Cairo') AS today,
                      (SELECT count(DISTINCT e) FROM linkedin.job_posts, unnest(contact_emails) e) AS emails,
                      (SELECT count(*) FROM (SELECT lower(e) FROM linkedin.job_posts, unnest(contact_emails) e GROUP BY 1
                          HAVING min(first_seen_at) >= date_trunc('day', now() AT TIME ZONE 'Africa/Cairo') AT TIME ZONE 'Africa/Cairo') n
                      ) AS emails_today,
                      max(first_seen_at) AS latest
               FROM linkedin.job_posts""")).fetchone()
    hb = state["agent_heartbeat_at"]
    online = hb is not None and (datetime.now(timezone.utc) - hb).total_seconds() < HEARTBEAT_STALE_SECONDS
    return {"state": state, "online": online, "counts": counts}


class Control(BaseModel):
    action: str


@app.post("/api/control")
async def control(body: Control, request: Request):
    if request.headers.get("origin") not in (None, f"https://{PUBLIC_HOST}"):
        raise HTTPException(403, "bad origin")
    if body.action not in ("start", "stop", "test", "now"):
        raise HTTPException(400, "action must be start, stop, test or now")
    desired = "stopped" if body.action == "stop" else "running"
    test = body.action == "test"
    who = user(request)
    async with pool.connection() as conn:
        await conn.execute(
            """UPDATE linkedin.scraper_state SET desired_state = %s, desired_by = %s, desired_at = now(),
                   run_until = CASE WHEN %s THEN now() + make_interval(mins => %s) END,
                   run_max_posts = CASE WHEN %s THEN %s END,
                   run_now = %s,
                   next_cycle_at = CASE WHEN %s THEN next_cycle_at ELSE NULL END
               WHERE id = 1""",
            (desired, who, test, TEST_RUN_MINUTES, test, TEST_RUN_MAX_POSTS, body.action == "now", body.action == "stop"))
        label = {"test": f"test run ({TEST_RUN_MINUTES} min / {TEST_RUN_MAX_POSTS} posts)",
                 "now": "run now (one cycle, outside the schedule)"}.get(body.action, body.action)
        await conn.execute("INSERT INTO linkedin.scraper_events (level, message) VALUES ('info', %s)",
                           (f"{label} requested by {who}",))
    return {"desired_state": desired}


@app.get("/api/posts")
async def posts(q: str = "", hiring_only: bool = True, ksa_remote: bool = True, saudi: str = "all", my_roles: bool = True,
                source: str = "all", egypt: str = "all", limit: int = 100, offset: int = 0):
    limit = max(1, min(limit, 2000))
    offset = max(offset, 0)
    where, params = ["TRUE"], {}
    if hiring_only:
        where.append("is_hiring")
    if source in ("linkedin", "telegram"):
        where.append(("" if source == "telegram" else "NOT ") + "starts_with(coalesce(search_keywords, ''), 'Telegram @')")
    if q:
        where.append("(content ILIKE %(q)s OR job_title ILIKE %(q)s OR ai_title ILIKE %(q)s OR company ILIKE %(q)s OR location ILIKE %(q)s"
                     " OR author_name ILIKE %(q)s OR author_headline ILIKE %(q)s OR array_to_string(contact_emails, ' ') ILIKE %(q)s)")
        params["q"] = f"%{q}%"
    async with pool.connection() as conn:
        rows = await (await conn.execute(
            f"""SELECT id, author_name, author_headline, author_profile_url, post_url, posted_ago, posted_at_est,
                       job_title, ai_title, company, location, job_url, contact_emails, search_keywords, is_hiring,
                       first_seen_at, last_seen_at, content, emailed_at, emailed_to, skipped_at,
                       ai_offer, ai_fit, ai_reason, ai_checked_at, job_description, job_fetched_at,
                       ai_match_score, ai_match_reason,
                       (SELECT count(*) FROM linkedin.job_posts x WHERE x.author_profile_url = p.author_profile_url) AS author_posts,
                       (SELECT max(x.emailed_at) FROM linkedin.job_posts x
                        WHERE x.author_profile_url = p.author_profile_url AND x.id <> p.id
                          AND NOT starts_with(p.author_profile_url, 'https://t.me/')) AS author_emailed_at,
                       (SELECT max(x.emailed_at) FROM linkedin.job_posts x
                        WHERE x.emailed_to && p.contact_emails AND x.id <> p.id) AS addr_emailed_at
                FROM linkedin.job_posts p WHERE {' AND '.join(where)}
                ORDER BY first_seen_at DESC, id DESC""", params)).fetchall()
        roles = None
        if my_roles:
            # Same role patterns the agent uses for the feed, as last reported by the agent.
            cfg = await (await conn.execute("SELECT search_config FROM linkedin.scraper_state LIMIT 1")).fetchone()
            pats = ((cfg or {}).get("search_config") or {}).get("feed_browse", {}).get("roles") or []
            roles = re.compile("|".join(pats), re.I) if pats else None
    if my_roles:
        # Haiku's verdict once the agent has screened the post; the role regex until then.
        rows = [r for r in rows if (r["ai_fit"] if r["ai_checked_at"] else
                                    not roles or roles.search(f"{r['job_title'] or ''} {r['ai_title'] or ''} {r['content'] or ''}"))]
    for r in rows:
        r.update(salary_of(f"{r['job_title'] or ''}\n{r['content'] or ''}"))
        r["content"] = (r["content"] or "")[:4000]
        r["country"] = post_country(r["location"], r["content"], r["search_keywords"])
        r["work_mode"] = work_mode_of(r["location"], r["content"])
        r["saudi_only"] = bool(SAUDI_ONLY_RE.search(f"{r['job_title'] or ''}\n{r['content']}"))
    if ksa_remote:
        # A post naming no place at all can't be ruled out; Haiku already checked it against the location criteria.
        rows = [r for r in rows if in_target(r["country"], r["content"]) or (r["country"] is None and r["ai_fit"])]
    if saudi in ("only", "hide"):
        rows = [r for r in rows if r["saudi_only"] == (saudi == "only")]
    if egypt == "hide":
        rows = [r for r in rows if r["country"] != "eg"]
    elif egypt in ("onsite", "remote"):
        # Hybrid still means showing up at an office in Egypt.
        rows = [r for r in rows if r["country"] == "eg" and (r["work_mode"] == "remote") == (egypt == "remote")]
    return rows[offset:offset + limit]


# Approximate USD rates for the Salaries tab (Gulf rates are pegged; others move, so treat them as rough).
FX_USD = {"USD": 1, "SAR": 0.2667, "AED": 0.2723, "QAR": 0.2747, "KWD": 3.25, "BHD": 2.653, "OMR": 2.6, "JOD": 1.41,
          "EGP": 0.0205, "EUR": 1.08, "GBP": 1.27, "CAD": 0.73, "AUD": 0.66, "INR": 0.012, "PKR": 0.0036}
PER_MONTH = {"month": 1, "year": 1 / 12, "week": 52 / 12, "day": 22, "hour": 160}
SALARY_GROUPS = {"family": "ai_family", "sector": "ai_sector", "seniority": "ai_seniority", "country": "NULL"}


def usd_month(amount, currency: str | None, period: str | None) -> float | None:
    rate = FX_USD.get((currency or "").upper())
    if amount is None or rate is None:
        return None
    return round(float(amount) * rate * PER_MONTH.get(period or "month", 1))


def _pct(xs: list[float], q: float) -> float | None:
    if not xs:
        return None
    xs = sorted(xs)
    i = (len(xs) - 1) * q
    lo = int(i)
    return round(xs[lo] + (xs[min(lo + 1, len(xs) - 1)] - xs[lo]) * (i - lo))


@app.get("/api/salaries")
async def salaries(group: str = "family", matching_only: bool = False, source: str = "all", country: str = "split"):
    """country: "split" = one row per group and country, "all" = countries combined, or a country code to keep only it."""
    col = SALARY_GROUPS.get(group)
    if not col:
        raise HTTPException(400, "group must be family, sector, seniority or country")
    where = ["is_hiring", "ai_offer", "ai_model IS NOT NULL"]
    if matching_only:
        where.append("ai_fit")
    if source in ("linkedin", "telegram"):
        where.append(("" if source == "telegram" else "NOT ") + "starts_with(coalesce(search_keywords, ''), 'Telegram @')")
    async with pool.connection() as conn:
        rows = await (await conn.execute(
            f"""SELECT id, coalesce(job_title, ai_title) AS title, company, location, author_name, post_url, ai_fit,
                       {col} AS grp, ai_family, ai_sector, ai_seniority, ai_sal_min, ai_sal_max, ai_sal_currency,
                       ai_sal_period, ai_sal_text, ai_est_min_usd, ai_est_max_usd, search_keywords, first_seen_at, content
                FROM linkedin.job_posts WHERE {' AND '.join(where)} ORDER BY first_seen_at DESC""")).fetchall()
        pending = (await (await conn.execute(
            "SELECT count(*) AS n FROM linkedin.job_posts WHERE is_hiring AND ai_model IS NULL")).fetchone())["n"]
    for r in rows:
        r["country"] = post_country(r["location"], (r.pop("content") or "")[:4000], r["search_keywords"]) or ""
    countries: dict[str, int] = {}
    for r in rows:
        countries[r["country"]] = countries.get(r["country"], 0) + 1
    if country not in ("split", "all"):
        rows = [r for r in rows if r["country"] == country]
    groups: dict[str, list] = {}
    for r in rows:
        name = r["country"] if group == "country" else r["grp"] or "Unclassified"
        groups.setdefault(name, []).append({
            **r, "title": (r["title"] or "").split("\n")[0],
            "usd_min": usd_month(r["ai_sal_min"], r["ai_sal_currency"], r["ai_sal_period"]),
            "usd_max": usd_month(r["ai_sal_max"], r["ai_sal_currency"], r["ai_sal_period"]),
            "est_min": r["ai_est_min_usd"] and round(float(r["ai_est_min_usd"])),
            "est_max": r["ai_est_max_usd"] and round(float(r["ai_est_max_usd"]))})

    def summary(name: str, posts: list) -> dict:
        stated = [p for p in posts if p["usd_min"] is not None]
        mids = [(p["usd_min"] + (p["usd_max"] or p["usd_min"])) / 2 for p in stated]
        est = [p for p in posts if p["est_min"] is not None]
        return {"name": name, "n": len(posts),
                "stated": {"n": len(stated), "min": min((p["usd_min"] for p in stated), default=None),
                           "max": max((p["usd_max"] or p["usd_min"] for p in stated), default=None),
                           "p25": _pct(mids, .25), "median": _pct(mids, .5), "p75": _pct(mids, .75)},
                "est": {"n": len(est), "min": _pct([p["est_min"] for p in est], .5),
                        "max": _pct([p["est_max"] or p["est_min"] for p in est], .5)},
                "posts": posts}

    score = lambda g: g["stated"]["median"] or g["est"]["max"] or 0
    out = []
    for name, posts in groups.items():
        g = summary(name, posts)
        if country == "split" and group != "country":
            per: dict[str, list] = {}
            for p in posts:
                per.setdefault(p["country"], []).append(p)
            g["by_country"] = sorted((summary(c, ps) for c, ps in per.items()), key=lambda s: (-score(s), s["name"]))
        out.append(g)
    out.sort(key=lambda g: (-score(g), g["name"]))
    return {"groups": out, "pending": pending, "fx": FX_USD,
            "countries": sorted(({"code": c, "n": n} for c, n in countries.items()), key=lambda c: -c["n"])}


@app.get("/api/posts/{post_id}")
async def post(post_id: int):
    async with pool.connection() as conn:
        p = await (await conn.execute(
            """SELECT id, author_name, author_headline, author_profile_url, post_url, posted_ago, posted_at_est,
                      job_title, ai_title, company, location, job_url, contact_emails, first_seen_at, content,
                      emailed_at, emailed_to, skipped_at, skipped_by, ai_offer, ai_fit, ai_reason, ai_checked_at,
                      job_description, job_fetched_at, ai_match_score, ai_match_reason
               FROM linkedin.job_posts WHERE id = %s""", (post_id,))).fetchone()
        if not p:
            raise HTTPException(404, "post not found")
        p["sent"] = await (await conn.execute(
            """SELECT id, to_addrs, cc_addrs, subject, attachment, status, error, sent_by, sent_at
               FROM linkedin.sent_emails WHERE post_id = %s ORDER BY sent_at DESC""",
            (post_id,))).fetchall()
        # Earlier emails to the same poster (other posts) or to the same address. A Telegram channel
        # reposts many employers, so there only the address counts.
        p["author_sent"] = await (await conn.execute(
            """SELECT s.post_id, s.to_addrs, s.subject, s.sent_at FROM linkedin.sent_emails s
               JOIN linkedin.job_posts x ON x.id = s.post_id
               WHERE s.status = 'sent' AND s.post_id <> %(id)s
                 AND ((x.author_profile_url = %(url)s AND NOT starts_with(%(url)s, 'https://t.me/'))
                      OR s.to_addrs && %(emails)s::text[])
               ORDER BY s.sent_at DESC LIMIT 5""",
            {"id": post_id, "url": p["author_profile_url"], "emails": p["contact_emails"]})).fetchall()
        p["author_posts"] = (await (await conn.execute(
            "SELECT count(*) AS n FROM linkedin.job_posts WHERE author_profile_url = %s",
            (p["author_profile_url"],))).fetchone())["n"]
    return p


class Skip(BaseModel):
    skip: bool = True


@app.post("/api/posts/{post_id}/skip")
async def skip_post(post_id: int, body: Skip, request: Request):
    if request.headers.get("origin") not in (None, f"https://{PUBLIC_HOST}"):
        raise HTTPException(403, "bad origin")
    who = user(request)
    async with pool.connection() as conn, conn.transaction():
        row = await (await conn.execute(
            """UPDATE linkedin.job_posts SET skipped_at = CASE WHEN %(s)s THEN now() END,
                                             skipped_by = CASE WHEN %(s)s THEN %(who)s END
               WHERE id = %(id)s RETURNING skipped_at""", {"s": body.skip, "who": who, "id": post_id})).fetchone()
        if not row:
            raise HTTPException(404, "post not found")
        text = f"post #{post_id} {'skipped' if body.skip else 'unskipped'} by {who}"
        await conn.execute("INSERT INTO linkedin.scraper_events (level, message) VALUES ('info', %s)", (text,))
    log.info(text)
    return row


def default_cv() -> Path | None:
    files = [f for f in CV_DIR.iterdir() if f.is_file() and f.suffix.lower() in ATTACH_TYPES] if CV_DIR.is_dir() else []
    return max(files, key=lambda f: f.stat().st_mtime, default=None)


@app.get("/api/mail/config")
async def mail_config(request: Request):
    cv = default_cv()
    method = "gmail-api" if gmail_oauth() else "smtp" if GMAIL_APP_PASSWORD else None
    return {"from": formataddr((MAIL_FROM_NAME, GMAIL_USER)) if GMAIL_USER else None,
            "configured": bool(GMAIL_USER and method), "method": method,
            "allowed": user(request).lower() in MAIL_USERS,
            "cv": {"filename": cv.name, "size": cv.stat().st_size} if cv else None}


@app.get("/api/cv")
async def cv_file():
    cv = default_cv()
    if not cv:
        raise HTTPException(404, "no CV on the server")
    return FileResponse(cv, media_type=ATTACH_TYPES[cv.suffix.lower()], filename=cv.name, content_disposition_type="inline")


class Attachment(BaseModel):
    filename: str
    data: str  # base64


class Mail(BaseModel):
    post_id: int | None = None
    to: list[str]
    cc: list[str] = []
    subject: str
    html: str
    text: str
    attach_cv: bool = True
    attachment: Attachment | None = None  # replaces the server CV for this email


def addresses(values: list[str], field: str) -> list[str]:
    out = [v.strip() for v in values if v.strip()]
    bad = next((v for v in out if not EMAIL_RE.fullmatch(v)), None)
    if bad:
        raise HTTPException(400, f"invalid {field} address: {bad[:80]}")
    return out


def smtp_send(msg: EmailMessage):
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
        s.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        s.send_message(msg)


class MailError(Exception):
    pass


def gmail_oauth() -> dict | None:
    try:
        cfg = json.loads(GMAIL_OAUTH_FILE.read_text())
    except (OSError, ValueError):
        return None
    return cfg if all(cfg.get(k) for k in ("client_id", "client_secret", "refresh_token")) else None


_access = {"token": "", "exp": 0.0}


def gmail_access_token(cfg: dict) -> str:
    if _access["token"] and time.time() < _access["exp"] - 60:
        return _access["token"]
    data = urllib.parse.urlencode({"client_id": cfg["client_id"], "client_secret": cfg["client_secret"],
                                   "refresh_token": cfg["refresh_token"], "grant_type": "refresh_token"}).encode()
    try:
        with urllib.request.urlopen("https://oauth2.googleapis.com/token", data=data, timeout=20) as r:
            tok = json.load(r)
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:200]
        raise MailError(f"Gmail sign-in expired or revoked; re-run deploy/gmail-oauth-login.py ({e.code} {detail})")
    _access.update(token=tok["access_token"], exp=time.time() + int(tok.get("expires_in", 3600)))
    return _access["token"]


def gmail_api_send(msg: EmailMessage, cfg: dict) -> str:
    req = urllib.request.Request(
        "https://gmail.googleapis.com/upload/gmail/v1/users/me/messages/send?uploadType=media",
        data=msg.as_bytes(), method="POST",
        headers={"Authorization": f"Bearer {gmail_access_token(cfg)}", "Content-Type": "message/rfc822"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r).get("id", "")
    except urllib.error.HTTPError as e:
        if e.code == 401:
            _access["token"] = ""
        raise MailError(f"Gmail API {e.code}: {e.read().decode(errors='replace')[:250]}")


@app.post("/api/send")
async def send(body: Mail, request: Request):
    if request.headers.get("origin") not in (None, f"https://{PUBLIC_HOST}"):
        raise HTTPException(403, "bad origin")
    who = user(request)
    if who.lower() not in MAIL_USERS:
        raise HTTPException(403, f"{who} may not send from {GMAIL_USER or 'the mailbox'}")
    oauth = gmail_oauth()
    if not GMAIL_USER or not (oauth or GMAIL_APP_PASSWORD):
        raise HTTPException(503, "Gmail is not configured on the server (run deploy/gmail-oauth-login.py)")
    to, cc = addresses(body.to, "To"), addresses(body.cc, "Cc")
    if not to or len(to) + len(cc) > 10:
        raise HTTPException(400, "need 1 to 10 recipients")
    subject = " ".join(body.subject.split())
    if not subject or len(subject) > 300:
        raise HTTPException(400, "subject must be 1-300 characters")
    if len(body.html) > 300_000 or len(body.text) > 100_000:
        raise HTTPException(400, "body too long")

    msg = EmailMessage()
    msg["From"] = formataddr((MAIL_FROM_NAME, GMAIL_USER))
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid(domain=GMAIL_USER.split("@")[-1])
    msg.set_content(body.text or " ")
    msg.add_alternative(body.html, subtype="html")

    attached = None
    if body.attachment:
        name = re.sub(r"[^\w .()-]", "_", Path(body.attachment.filename).name)[:120]
        ctype = ATTACH_TYPES.get(Path(name).suffix.lower())
        if not ctype:
            raise HTTPException(400, "attachment must be PDF, DOC or DOCX")
        try:
            data = base64.b64decode(body.attachment.data, validate=True)
        except binascii.Error:
            raise HTTPException(400, "attachment is not valid base64")
        attached = (name, ctype, data)
    elif body.attach_cv:
        cv = default_cv()
        if not cv:
            raise HTTPException(400, "no CV on the server; attach one")
        attached = (cv.name, ATTACH_TYPES[cv.suffix.lower()], cv.read_bytes())
    if attached:
        name, ctype, data = attached
        if len(data) > MAX_ATTACHMENT:
            raise HTTPException(400, "attachment is over 5 MB")
        maintype, subtype = ctype.split("/")
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)

    async def record(status: str, error: str | None = None) -> dict:
        """Log the attempt; on success also mark the post as emailed. One transaction."""
        async with pool.connection() as conn, conn.transaction():
            row = await (await conn.execute(
                """INSERT INTO linkedin.sent_emails (post_id, to_addrs, cc_addrs, subject, attachment, attachment_bytes,
                                                     message_id, sent_by, status, error, body_html, body_text)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id, sent_at""",
                (body.post_id, to, cc, subject, attached[0] if attached else None, len(attached[2]) if attached else None,
                 msg["Message-ID"], who, status, error, body.html, body.text))).fetchone()
            if status == "sent" and body.post_id is not None:
                await conn.execute("UPDATE linkedin.job_posts SET emailed_at = %s, emailed_to = %s WHERE id = %s",
                                   (row["sent_at"], to, body.post_id))
            text = (f"email {status} to {', '.join(to)} (post #{body.post_id}, log #{row['id']}) by {who}"
                    + (f": {error}" if error else ""))
            await conn.execute("INSERT INTO linkedin.scraper_events (level, message) VALUES (%s, %s)",
                               ("info" if status == "sent" else "error", text[:500]))
        (log.info if status == "sent" else log.error)("%s; subject=%r message_id=%s attachment=%s",
                                                     text, subject, msg["Message-ID"], attached[0] if attached else None)
        return row

    try:
        if oauth:
            await asyncio.to_thread(gmail_api_send, msg, oauth)
        else:
            await asyncio.to_thread(smtp_send, msg)
    except MailError as e:
        await record("failed", str(e)[:300])
        raise HTTPException(502, str(e)[:300])
    except smtplib.SMTPAuthenticationError:
        await record("failed", "Gmail rejected the app password")
        raise HTTPException(502, "Gmail rejected the app password")
    except (smtplib.SMTPException, OSError) as e:
        err = f"Gmail send failed: {e}"[:300]
        await record("failed", err)
        raise HTTPException(502, err)

    try:
        row = await record("sent")
    except Exception:
        # The email is already out; never report it as failed (the user would resend).
        log.exception("email sent to %s (post #%s) but logging it to the DB failed", to, body.post_id)
        return {"sent_at": datetime.now(timezone.utc), "attachment": attached[0] if attached else None, "logged": False}
    return {"sent_at": row["sent_at"], "attachment": attached[0] if attached else None, "logged": True, "log_id": row["id"]}


@app.get("/api/events")
async def events(limit: int = 50):
    async with pool.connection() as conn:
        return await (await conn.execute(
            "SELECT at, level, message FROM linkedin.scraper_events ORDER BY id DESC LIMIT %s",
            (max(1, min(limit, 200)),))).fetchall()

import boto3

FAMILIES = ["AI / ML engineering", "AI / data leadership", "Solution / enterprise architecture", "Cloud / DevOps / infrastructure",
            "Software engineering", "Data engineering / analytics", "Product management", "CTO / technology executive",
            "IT management / operations", "Cybersecurity", "Telecom / CPaaS", "Digital transformation / consulting",
            "Presales / sales engineering", "Other"]
SECTORS = ["Technology / SaaS", "AI", "Telecom", "Banking / fintech / insurance", "Government / public sector", "Healthcare",
           "Energy / oil & gas", "Consulting / IT services", "Retail / e-commerce", "Real estate / construction", "Education",
           "Media / entertainment", "Logistics / transport", "Hospitality / travel", "Recruitment agency (client undisclosed)",
           "Other / unknown"]
SENIORITY = ["Junior", "Mid", "Senior", "Lead / principal", "Manager / head", "Director / VP", "C-level"]
PERIODS = ["month", "year", "week", "day", "hour"]

SYSTEM = ("You screen job posts (LinkedIn, Telegram) for a job seeker. Decide (1) whether the post is a real job offer: an employer, "
          "hiring manager or recruiter advertising an open position. Candidates promoting themselves or someone else "
          "(\"open to work\", \"spotlighting talent\"), job-market commentary, courses, events and generic career "
          "advice are NOT job offers. (2) Whether the offered role fits the seeker's criteria below. "
          "Judge only what the post states: unknown location, remote or visa details are NOT a reason to reject. "
          "Also extract the job title(s) being hired for as short titles (e.g. \"Senior Solution Architect\"), "
          "no company, location, emojis or hashtags; up to 3; empty if no specific role is named. "
          "Classify the role (family, sector of the hiring company, seniority). Record a salary only if the post states one, "
          "exactly as written (amounts, currency, period). If no salary is stated, give your estimate of the typical gross "
          "monthly pay in USD for this role, seniority and location in 2026; it is shown labelled as an estimate.\n\n"
          "Seeker's criteria:\n{criteria}")
_RANGE = {"type": "object", "properties": {"min": {"type": "number"}, "max": {"type": "number"}}}
TOOL = {"toolSpec": {
    "name": "screen_post",
    "description": "Record the screening result for the post.",
    "inputSchema": {"json": {"type": "object", "required": ["titles", "job_offer", "fits", "reason"], "properties": {
        "titles": {"type": "array", "items": {"type": "string"}},
        "job_offer": {"type": "boolean", "description": "An open position is being advertised"},
        "fits": {"type": "boolean", "description": "A job offer whose role matches the criteria and whose stated seniority and location (if any) do not conflict with them"},
        "reason": {"type": "string", "description": "One short sentence explaining the verdict"},
        "family": {"type": "string", "enum": FAMILIES},
        "sector": {"type": "string", "enum": SECTORS, "description": "Sector of the hiring company"},
        "seniority": {"type": "string", "enum": SENIORITY},
        "salary": {"type": "object", "description": "Only when the post states pay; omit otherwise", "properties": {
            "min": {"type": "number"}, "max": {"type": "number"},
            "currency": {"type": "string", "description": "ISO 4217 code, e.g. SAR, USD, EGP"},
            "period": {"type": "string", "enum": PERIODS},
            "text": {"type": "string", "description": "The salary as written in the post"}}},
        "estimate_usd_month": {**_RANGE, "description": "Only when no salary is stated and it is a job offer: typical gross monthly USD"},
    }}},
}}


def _num(v):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def _range(d):
    lo, hi = _num((d or {}).get("min")), _num((d or {}).get("max"))
    lo = lo or hi
    if hi is not None and lo is not None and hi < lo:
        lo, hi = hi, lo
    return lo, hi if hi != lo else None


def classification(v: dict) -> dict:
    sal = v.get("salary") or {}
    lo, hi = _range(sal)
    cur = str(sal.get("currency") or "").strip().upper()
    stated = lo is not None and len(cur) == 3 and cur.isalpha()
    est_lo, est_hi = (None, None) if stated else _range(v.get("estimate_usd_month"))
    pick = lambda val, allowed: val if val in allowed else None
    return {"family": pick(v.get("family"), FAMILIES), "sector": pick(v.get("sector"), SECTORS),
            "seniority": pick(v.get("seniority"), SENIORITY),
            "sal_min": lo if stated else None, "sal_max": hi if stated else None, "sal_currency": cur if stated else None,
            "sal_period": (pick(sal.get("period"), PERIODS) or "month") if stated else None,
            "sal_text": str(sal.get("text") or "")[:200] or None if stated else None,
            "est_min": est_lo, "est_max": est_hi}


class PostScreener:
    def __init__(self, model: str, profile: str, region: str, criteria: str):
        self.model = model
        self.system = SYSTEM.format(criteria=criteria)
        self.client = boto3.Session(profile_name=profile, region_name=region).client("bedrock-runtime")

    def __call__(self, content: str) -> dict:
        r = self.client.converse(
            modelId=self.model, system=[{"text": self.system}],
            messages=[{"role": "user", "content": [{"text": content[:4000]}]}],
            toolConfig={"tools": [TOOL], "toolChoice": {"tool": {"name": "screen_post"}}},
            inferenceConfig={"maxTokens": 500})  # Haiku 5.5 rejects `temperature`
        for c in r["output"]["message"]["content"]:
            if "toolUse" in c:
                v = c["toolUse"]["input"]
                titles = [t.strip() for t in v.get("titles", []) if t.strip()]
                offer = bool(v.get("job_offer"))
                return {"title": "\n".join(titles[:3])[:200], "offer": offer, "fit": offer and bool(v.get("fits")),
                        "reason": str(v.get("reason", ""))[:300], "model": self.model, **classification(v)}
        raise RuntimeError("model returned no screening result")


MATCH_SYSTEM = ("You compare a job posting with a candidate's CV and score how well the candidate matches the job, "
                "0-100, weighing required skills and experience, seniority, domain and location/eligibility. "
                "If only a short hiring post is given, score on what it states; don't penalise details it omits. "
                "90+ = meets nearly every requirement; 70-89 = good match with minor gaps; 50-69 = partial match; "
                "below 50 = weak match. Be calibrated and strict. Ignore LinkedIn page chrome (Premium ads, "
                "\"people you can reach out to\", applicant counts).\n\nCandidate CV:\n{cv}")
MATCH_TOOL = {"toolSpec": {
    "name": "score_match",
    "description": "Record how well the CV matches the job.",
    "inputSchema": {"json": {"type": "object", "required": ["score", "reason"], "properties": {
        "score": {"type": "integer", "minimum": 0, "maximum": 100},
        "reason": {"type": "string", "description": "One short sentence: the main strengths and gaps"}}}},
}}


class CvMatcher:
    def __init__(self, client, model: str):
        self.client, self.model = client, model

    def __call__(self, cv: str, job: str) -> dict:
        r = self.client.converse(
            modelId=self.model, system=[{"text": MATCH_SYSTEM.format(cv=cv[:30000])}],
            messages=[{"role": "user", "content": [{"text": job[:20000]}]}],
            toolConfig={"tools": [MATCH_TOOL], "toolChoice": {"tool": {"name": "score_match"}}},
            inferenceConfig={"maxTokens": 300})
        for c in r["output"]["message"]["content"]:
            if "toolUse" in c:
                v = c["toolUse"]["input"]
                score = _num(v.get("score")) or 0
                return {"score": max(0, min(100, round(score))), "reason": str(v.get("reason", ""))[:300]}
        raise RuntimeError("model returned no match score")

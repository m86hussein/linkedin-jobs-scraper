-- Tech recruiters found by LinkedIn people search; the agent reads their recent posts like post authors.
CREATE TABLE IF NOT EXISTS linkedin.recruiters (
    profile_url text PRIMARY KEY,
    name        text,
    headline    text,
    location    text,
    found_by    text,
    found_at    timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS linkedin.recruiter_searches (
    id          bigserial PRIMARY KEY,
    query       text NOT NULL,
    searched_at timestamptz NOT NULL DEFAULT now(),
    results     integer,
    kept        integer,
    new         integer
);
GRANT SELECT, INSERT ON linkedin.recruiters, linkedin.recruiter_searches TO linkedin_scraper;
GRANT USAGE, SELECT ON SEQUENCE linkedin.recruiter_searches_id_seq TO linkedin_scraper;

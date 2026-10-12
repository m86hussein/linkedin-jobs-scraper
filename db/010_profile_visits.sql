-- Recruiter profiles the agent read (recent posts), so each is revisited at most every N days.
CREATE TABLE IF NOT EXISTS linkedin.profile_visits (
    id             bigserial PRIMARY KEY,
    profile_url    text NOT NULL,
    visited_at     timestamptz NOT NULL DEFAULT now(),
    posts_seen     integer,
    posts_matching integer,
    posts_new      integer
);
CREATE INDEX IF NOT EXISTS profile_visits_url_at ON linkedin.profile_visits (profile_url, visited_at DESC);
GRANT SELECT, INSERT ON linkedin.profile_visits TO linkedin_scraper;
GRANT USAGE, SELECT ON SEQUENCE linkedin.profile_visits_id_seq TO linkedin_scraper;

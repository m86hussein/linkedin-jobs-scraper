-- CV text (extracted by the dashboard from the server CV) and Haiku's CV-to-job match score for read LinkedIn jobs.
CREATE TABLE IF NOT EXISTS linkedin.cv_profile (
    id          int PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    filename    text NOT NULL,
    cv_text     text NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now()
);
GRANT SELECT, INSERT, UPDATE ON linkedin.cv_profile TO linkedin_scraper;
ALTER TABLE linkedin.job_posts ADD COLUMN IF NOT EXISTS ai_match_score smallint;
ALTER TABLE linkedin.job_posts ADD COLUMN IF NOT EXISTS ai_match_reason text;
ALTER TABLE linkedin.job_posts ADD COLUMN IF NOT EXISTS ai_match_at timestamptz;

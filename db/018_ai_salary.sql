-- Haiku classification for the Salaries tab: role family, sector, seniority,
-- the salary the post states (as written) and a labelled estimate when none is stated.
ALTER TABLE linkedin.job_posts ADD COLUMN IF NOT EXISTS ai_family text;
ALTER TABLE linkedin.job_posts ADD COLUMN IF NOT EXISTS ai_sector text;
ALTER TABLE linkedin.job_posts ADD COLUMN IF NOT EXISTS ai_seniority text;
ALTER TABLE linkedin.job_posts ADD COLUMN IF NOT EXISTS ai_sal_min numeric;
ALTER TABLE linkedin.job_posts ADD COLUMN IF NOT EXISTS ai_sal_max numeric;
ALTER TABLE linkedin.job_posts ADD COLUMN IF NOT EXISTS ai_sal_currency text;
ALTER TABLE linkedin.job_posts ADD COLUMN IF NOT EXISTS ai_sal_period text;
ALTER TABLE linkedin.job_posts ADD COLUMN IF NOT EXISTS ai_sal_text text;
ALTER TABLE linkedin.job_posts ADD COLUMN IF NOT EXISTS ai_est_min_usd numeric;
ALTER TABLE linkedin.job_posts ADD COLUMN IF NOT EXISTS ai_est_max_usd numeric;
ALTER TABLE linkedin.job_posts ADD COLUMN IF NOT EXISTS ai_model text;

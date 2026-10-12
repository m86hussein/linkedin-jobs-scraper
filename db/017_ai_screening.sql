-- Haiku screening: is the post a real job offer, and does it fit the target criteria?
ALTER TABLE linkedin.job_posts ADD COLUMN IF NOT EXISTS ai_offer boolean;
ALTER TABLE linkedin.job_posts ADD COLUMN IF NOT EXISTS ai_fit boolean;
ALTER TABLE linkedin.job_posts ADD COLUMN IF NOT EXISTS ai_reason text;
ALTER TABLE linkedin.job_posts ADD COLUMN IF NOT EXISTS ai_checked_at timestamptz;

-- The linked LinkedIn job posting ("About the job" etc.), read by the agent for posts without an email.
ALTER TABLE linkedin.job_posts ADD COLUMN IF NOT EXISTS job_description text;
ALTER TABLE linkedin.job_posts ADD COLUMN IF NOT EXISTS job_fetched_at timestamptz;
ALTER TABLE linkedin.job_posts ADD COLUMN IF NOT EXISTS job_fetch_error text;

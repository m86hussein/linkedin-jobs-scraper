-- Job title extracted by an LLM for posts without a LinkedIn job card; '' means none found.
ALTER TABLE linkedin.job_posts ADD COLUMN IF NOT EXISTS ai_title text;

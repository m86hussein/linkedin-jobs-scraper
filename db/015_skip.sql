-- Posts dismissed from the Compose tab ("Skip").
ALTER TABLE linkedin.job_posts
    ADD COLUMN IF NOT EXISTS skipped_at timestamptz,
    ADD COLUMN IF NOT EXISTS skipped_by text;

ALTER TABLE linkedin.scraper_state
    ADD COLUMN IF NOT EXISTS run_until     timestamptz,
    ADD COLUMN IF NOT EXISTS run_max_posts integer;

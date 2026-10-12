-- "Run now": one cycle immediately, ignoring active hours, the schedule and the daily cap.
ALTER TABLE linkedin.scraper_state ADD COLUMN IF NOT EXISTS run_now boolean NOT NULL DEFAULT false;

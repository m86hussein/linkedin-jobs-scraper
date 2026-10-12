-- Dashboard view settings (column widths, sort, filters) per signed-in user, so they follow them across browsers.
CREATE TABLE IF NOT EXISTS linkedin.dashboard_prefs (
    user_email  text PRIMARY KEY,
    prefs       jsonb NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now()
);
GRANT SELECT, INSERT, UPDATE ON linkedin.dashboard_prefs TO linkedin_scraper;

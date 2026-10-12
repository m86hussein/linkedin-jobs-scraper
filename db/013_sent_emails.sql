-- Applications emailed from the dashboard's Compose tab.
CREATE TABLE IF NOT EXISTS linkedin.sent_emails (
    id          bigserial PRIMARY KEY,
    post_id     bigint REFERENCES linkedin.job_posts(id),
    to_addrs    text[] NOT NULL,
    cc_addrs    text[] NOT NULL DEFAULT '{}',
    subject     text NOT NULL,
    attachment  text,
    message_id  text,
    sent_by     text,
    sent_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS sent_emails_post_id_idx ON linkedin.sent_emails (post_id);
GRANT SELECT, INSERT ON linkedin.sent_emails TO linkedin_scraper;
GRANT USAGE, SELECT ON SEQUENCE linkedin.sent_emails_id_seq TO linkedin_scraper;

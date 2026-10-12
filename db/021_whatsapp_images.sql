-- LinkedIn post screenshots sent on WhatsApp; one row per image so each is handled once.
CREATE TABLE IF NOT EXISTS linkedin.whatsapp_images (
    message_id   text PRIMARY KEY,
    chat_jid     text NOT NULL,
    received_at  timestamptz NOT NULL,
    status       text NOT NULL,  -- saved | duplicate | not_a_post | failed
    post_id      bigint REFERENCES linkedin.job_posts(id),
    detail       text,
    processed_at timestamptz NOT NULL DEFAULT now()
);
GRANT SELECT, INSERT ON linkedin.whatsapp_images TO linkedin_scraper;

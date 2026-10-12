-- Mark posts as emailed, and keep a full log of every send attempt (body included).
ALTER TABLE linkedin.job_posts
    ADD COLUMN IF NOT EXISTS emailed_at timestamptz,
    ADD COLUMN IF NOT EXISTS emailed_to text[];

ALTER TABLE linkedin.sent_emails
    ADD COLUMN IF NOT EXISTS status     text NOT NULL DEFAULT 'sent',
    ADD COLUMN IF NOT EXISTS error      text,
    ADD COLUMN IF NOT EXISTS body_html  text,
    ADD COLUMN IF NOT EXISTS body_text  text,
    ADD COLUMN IF NOT EXISTS attachment_bytes integer;

DO $$ BEGIN
    ALTER TABLE linkedin.sent_emails ADD CONSTRAINT sent_emails_status_chk CHECK (status IN ('sent', 'failed'));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

UPDATE linkedin.job_posts p SET emailed_at = s.sent_at, emailed_to = s.to_addrs
FROM (SELECT DISTINCT ON (post_id) post_id, sent_at, to_addrs FROM linkedin.sent_emails
      WHERE status = 'sent' AND post_id IS NOT NULL ORDER BY post_id, sent_at DESC) s
WHERE p.id = s.post_id AND p.emailed_at IS DISTINCT FROM s.sent_at;

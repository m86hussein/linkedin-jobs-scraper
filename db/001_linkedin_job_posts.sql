CREATE SCHEMA IF NOT EXISTS linkedin;

CREATE TABLE IF NOT EXISTS linkedin.job_posts (
    id                  bigserial PRIMARY KEY,
    -- Same job is often reposted by several recruiters; hash of normalised text dedupes them.
    content_hash        text        NOT NULL UNIQUE,
    post_url            text,
    author_name         text,
    author_headline     text,
    author_profile_url  text,
    posted_ago          text,
    posted_at_est       timestamptz,
    content             text        NOT NULL,
    job_title           text,
    company             text,
    location            text,
    job_url             text,
    contact_emails      text[]      NOT NULL DEFAULT '{}',
    search_keywords     text        NOT NULL,
    is_hiring           boolean     NOT NULL DEFAULT true,
    first_seen_at       timestamptz NOT NULL DEFAULT now(),
    last_seen_at        timestamptz NOT NULL DEFAULT now(),
    exported_to_sheet_at timestamptz
);

CREATE INDEX IF NOT EXISTS job_posts_first_seen_idx ON linkedin.job_posts (first_seen_at DESC);
CREATE INDEX IF NOT EXISTS job_posts_unexported_idx ON linkedin.job_posts (id) WHERE exported_to_sheet_at IS NULL;

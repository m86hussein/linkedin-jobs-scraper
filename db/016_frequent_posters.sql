-- Frequent posters: index for per-author counts, and add authors of 3+ hiring posts to the recruiters list.
CREATE INDEX IF NOT EXISTS job_posts_author_idx ON linkedin.job_posts (author_profile_url);

INSERT INTO linkedin.recruiters (profile_url, name, headline, found_by)
SELECT author_profile_url, max(author_name), max(author_headline), 'frequent poster'
FROM linkedin.job_posts
WHERE is_hiring AND author_profile_url LIKE 'https://www.linkedin.com/in/%'
GROUP BY author_profile_url HAVING count(*) >= 3
ON CONFLICT (profile_url) DO NOTHING;

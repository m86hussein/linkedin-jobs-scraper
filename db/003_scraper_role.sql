-- Least-privilege login used by the dashboard and the agent; password is set out-of-band.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'linkedin_scraper') THEN
        CREATE ROLE linkedin_scraper LOGIN;
    END IF;
END $$;

REVOKE ALL ON DATABASE appdb FROM linkedin_scraper;
GRANT CONNECT ON DATABASE appdb TO linkedin_scraper;
GRANT USAGE ON SCHEMA linkedin TO linkedin_scraper;
GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA linkedin TO linkedin_scraper;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA linkedin TO linkedin_scraper;

ALTER TABLE linkedin.scraper_state
    ADD COLUMN IF NOT EXISTS agent_ip_country text,
    ADD COLUMN IF NOT EXISTS agent_ip_city    text,
    ADD COLUMN IF NOT EXISTS agent_ip_org     text;

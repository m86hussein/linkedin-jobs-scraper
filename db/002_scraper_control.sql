-- Single-row control/status shared by the dashboard (EC2) and the scraper agent (Windows VM).
CREATE TABLE IF NOT EXISTS linkedin.scraper_state (
    id                  smallint    PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    desired_state       text        NOT NULL DEFAULT 'stopped' CHECK (desired_state IN ('running', 'stopped')),
    desired_by          text,
    desired_at          timestamptz NOT NULL DEFAULT now(),
    agent_state         text        NOT NULL DEFAULT 'offline',
    agent_host          text,
    agent_heartbeat_at  timestamptz,
    current_query       text,
    current_step        text,
    cycle_started_at    timestamptz,
    next_cycle_at       timestamptz,
    cycle_posts_seen    integer     NOT NULL DEFAULT 0,
    cycle_posts_new     integer     NOT NULL DEFAULT 0,
    searches_today      integer     NOT NULL DEFAULT 0,
    searches_day        date,
    last_error          text
);
INSERT INTO linkedin.scraper_state (id) VALUES (1) ON CONFLICT DO NOTHING;

CREATE TABLE IF NOT EXISTS linkedin.scraper_events (
    id      bigserial   PRIMARY KEY,
    at      timestamptz NOT NULL DEFAULT now(),
    level   text        NOT NULL DEFAULT 'info',
    message text        NOT NULL
);
CREATE INDEX IF NOT EXISTS scraper_events_at_idx ON linkedin.scraper_events (at DESC);

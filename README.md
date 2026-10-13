# linkedin-jobs-scraper

Collects LinkedIn hiring posts that match the roles you are looking for, screens them with an LLM, and shows them
in a small web dashboard where you can review each post and email the poster directly.

## Architecture

### 1 · Collection — runs on your PC

The agent uses your own signed-in browser, so LinkedIn sees a normal user session. Every arrow is labelled with the data it carries.

```mermaid
%%{init: {"theme": "dark", "flowchart": {"curve": "basis", "nodeSpacing": 40, "rankSpacing": 70}}}%%
flowchart LR
    subgraph SRC["🌐 Sources"]
        LI(["LinkedIn"])
        TGC(["Telegram<br/>public channels"])
    end

    subgraph PC["🖥️ Your PC · agent"]
        MCP["mcp-server-linkedin<br/>your signed-in browser"]
        TG["telegram.py"]
        AG["agent.py<br/>parse · dedupe · screen"]
    end

    subgraph AWS["☁️ AWS"]
        BR["🧠 Bedrock<br/>Claude Haiku"]
        DB[("PostgreSQL<br/>on EC2")]
        SM["🔑 Secrets Manager"]
    end

    LI -- "hiring posts · job details<br/>read-only MCP tools" --> MCP
    MCP -- "post text · author · job URL" --> AG
    TGC -- "channel posts · HTTPS" --> TG
    TG -- "posts" --> AG
    AG -- "get DB password" --> SM
    AG <-- "post + CV  →<br/>←  fit · salary · match score" --> BR
    AG <== "SSH tunnel :5432<br/>job_posts · heartbeat  →<br/>←  Start/Stop · run now · CV" ==> DB

    classDef src fill:#0c2a4d,stroke:#58a6ff,color:#e6edf3
    classDef ai fill:#2e1065,stroke:#a78bfa,color:#ede9fe
    classDef store fill:#052e16,stroke:#3fb950,color:#d1fae5
    class LI,TGC src
    class BR ai
    class DB,SM store
```

### 2 · Server — dashboard, WhatsApp ingest, deploy

```mermaid
%%{init: {"theme": "dark", "flowchart": {"curve": "basis", "nodeSpacing": 40, "rankSpacing": 70}}}%%
flowchart LR
    U(["👤 You"])
    WA(["📱 WhatsApp<br/>screenshot you forward"])
    GHA["⚙️ GitHub Actions"]

    subgraph EC2["☁️ EC2 server"]
        NG["nginx · TLS"]
        OP["oauth2-proxy<br/>Google sign-in"]
        DASH["FastAPI dashboard"]
        BRG["WhatsApp bridge"]
        WI["WhatsApp ingest<br/>linkedin_images.py"]
        DB[("PostgreSQL<br/>linkedin.*")]
    end

    BR["🧠 Bedrock<br/>Claude Haiku"]
    GM(["📧 Gmail API"])

    U -- "HTTPS" --> NG
    NG -- "auth_request" --> OP
    OP -- "verified email header" --> DASH
    DASH <-- "Start/Stop · CV  →<br/>←  jobs · salaries · status" --> DB
    DASH -- "email + CV attachment" --> GM

    WA -- "image message" --> BRG
    BRG -- "push hook" --> WI
    WI <-- "screenshot  →<br/>←  role · company · email" --> BR
    WI -- "INSERT job_posts" --> DB
    WI -. "reply: saved / duplicate" .-> BRG

    GHA -. "OIDC → SSM:<br/>pull · migrate · restart" .-> NG

    classDef src fill:#0c2a4d,stroke:#58a6ff,color:#e6edf3
    classDef ai fill:#2e1065,stroke:#a78bfa,color:#ede9fe
    classDef store fill:#052e16,stroke:#3fb950,color:#d1fae5
    classDef edge fill:#3b0764,stroke:#f472b6,color:#fce7f3
    class U,WA,GM src
    class BR ai
    class DB store
    class NG,OP edge
```

### 3 · Life of one post

```mermaid
%%{init: {"theme": "dark"}}%%
sequenceDiagram
    autonumber
    participant A as Agent
    participant L as LinkedIn (MCP)
    participant H as Bedrock Haiku
    participant D as PostgreSQL
    participant W as Dashboard
    actor Y as You
    A->>L: search_posts(query) on a paced schedule
    L-->>A: posts (text, author, links)
    A->>A: parse emails, dedupe by content hash
    A->>H: post + match_criteria
    H-->>A: is it an offer? does it fit? title, salary
    A->>H: job description + your CV
    H-->>A: match score 0–100 + reason
    A->>D: INSERT job_posts (through SSH tunnel)
    Y->>W: open dashboard (Google sign-in)
    W->>D: filtered jobs
    D-->>W: matching offers
    Y->>W: Compose → Send
    W->>W: subject "Role [Your Name]" + CV attached
    W-->>Y: sent ✓ (logged in sent_emails)
```

**Design notes**

- **Read-only on LinkedIn.** The agent can only call the tools in `ALLOWED_TOOLS`; it never posts, likes, or messages.
- **No inbound ports on the agent.** It reaches the database through an outbound SSH tunnel as a restricted user that can only forward to `127.0.0.1:5432`.
- **Push, not polling, for WhatsApp.** The bridge pushes each new image to the ingest hook; there is no loop scanning for messages.
- **Secrets stay out of git.** The DB password comes from Secrets Manager, Gmail credentials from a one-time OAuth login on the server.

## Folders

| Folder | What |
|---|---|
| [`agent/`](agent/) | Scraper agent that runs on a home or office PC, using your own signed-in LinkedIn browser session via [`mcp-server-linkedin`](https://pypi.org/project/mcp-server-linkedin/). **[Setup →](agent/README.md)** |
| [`dashboard/`](dashboard/) | FastAPI dashboard (status, Start/Stop, jobs grid, compose & send) behind Google sign-in (oauth2-proxy) |
| [`db/`](db/) | Postgres schema `linkedin` (migrations applied on every deploy) |
| [`deploy/`](deploy/) | Server setup and the deployer run by GitHub Actions |
| [`whatsapp/`](whatsapp/) | Optional: turns LinkedIn screenshots sent to a WhatsApp number into job posts |

## Configuration

- **Search & screening:** edit [`agent/config.json`](agent/config.json) — search queries, pacing, active hours,
  and `match_criteria` (plain-language description of the roles you want). Machine-specific overrides go in
  `agent/config.local.json` (git-ignored).
- **Email signature:** set your name and phone numbers in `ME` in `dashboard/static/index.html`, and
  `MAIL_FROM_NAME` / `GMAIL_USER` / `MAIL_ALLOWED_USERS` in the server environment.
- **Deploy (optional):** the GitHub Actions workflow deploys over AWS SSM using OIDC. Set repository variables
  `AWS_DEPLOY_ROLE_ARN`, `AWS_REGION` and `EC2_INSTANCE_ID`; without them the deploy job is skipped.
- **Secrets** are never stored in the repo: the database password comes from AWS Secrets Manager
  (`db.secret_id` in the agent config) or `/etc/jobsscrap/env` on the server; Gmail credentials from
  `deploy/gmail-oauth-login.py`.

## Responsible use

This tool automates a browser session that you are signed into. Use it only with your own account, at a gentle
pace (the defaults deliberately space requests minutes apart and cap daily searches), and in line with
LinkedIn's User Agreement and the privacy rights of the people whose posts you collect. The agent is restricted
to read-only tools (`ALLOWED_TOOLS` in `agent/agent.py`). You are responsible for how you use it.

## License

[MIT](LICENSE)

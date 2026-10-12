# Project rules

## ALWAYS event-driven — NEVER polling (owner's standing rule)

New or changed services must react to pushed events (webhooks, WhatsApp bridge hooks,
queues, notifications). Do **not** write `while True: … sleep(N)` loops, timers or
cron jobs that re-scan a DB/API to discover new work.

Allowed: one bounded catch-up pass at startup; capped retry-with-backoff of a specific
failed item; short pacing sleeps between outbound requests.

## WhatsApp image ingest (`whatsapp/linkedin_images.py`)

- Fed by the WhatsApp bridge `MEDIA_HOOK_URL` push (comma-separated fan-out) on
  `127.0.0.1:8097/bridge-media`. Payload: `id, chat_jid, sender, media_type, filename,
  path, content, is_from_me, timestamp`.
- Bridge source + hook docs: private repo `m86hussein/whatsapp-mcp`
  (`.github/copilot-instructions.md`). Other hook consumers: 8094 infobip-forwarder,
  8098 voice-transcriber — append URLs, never replace.

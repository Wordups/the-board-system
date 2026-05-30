# Ask The Board — proxy

Cloudflare Worker that sits between the front-end chat widget and the
Anthropic API. The Anthropic API key lives **only here, as a Worker secret** —
never in the browser, never in the repo.

```
browser widget  ──POST {question, board, history}──►  worker /ask  ──►  Anthropic /v1/messages
                                                       (holds key)
```

## Deploy

```bash
cd worker
npm install -g wrangler                  # one-time, if needed
wrangler login                           # opens browser; sign into Cloudflare
wrangler secret put ANTHROPIC_API_KEY    # paste your key when prompted
wrangler deploy                          # publishes the worker
```

After deploy, wrangler prints the public URL — something like
`https://ask-the-board.<account>.workers.dev`. **Save it** — the front-end
widget needs that URL as its `PROXY_URL` config constant.

## Test (sample board)

```bash
curl -X POST https://ask-the-board.<account>.workers.dev/ask \
  -H 'Content-Type: application/json' \
  -H 'Origin: https://wordups.github.io' \
  -d '{
    "question": "What is the best HR play tonight?",
    "board": {
      "date": "2026-05-30",
      "sport": "MLB",
      "pinned_board": {
        "title": "HR Core",
        "players": [
          {"player_name": "Aaron Judge", "team": "NYY", "opponent": "BOS",
           "line": "HR 1+", "score": 39.1, "sim_prob_pct": 28.3, "tier": "A"}
        ]
      }
    },
    "history": []
  }'
```

Expected: `{ "answer": "..." }` that names Judge, his exact line, and the
tier label. Then try a grounding negative:

```bash
curl ... -d '{
  "question": "What is Aaron Judge''s exit velocity tonight?",
  "board": { ... same as above ... },
  "history": []
}'
```

The answer **must** be: `"I don't have that in today's board."` (or
equivalent wording with that exact intent). If the bot invents a number,
the grounding rule failed — let me know and I'll tighten the system prompt.

## CORS

`ALLOWED_ORIGINS` in `ask.js` is pinned to:

- `https://wordups.github.io` (Pages production)
- `http://localhost:8000` and `http://127.0.0.1:8000` (local preview)

Add a custom domain if/when you have one, and redeploy.

## Cost notes

- Model: `claude-sonnet-4-6` (chat-priced — cheap enough to run a real chat).
- `max_tokens: 1024` per reply (raise/lower in `ask.js` if needed).
- **Prompt caching** is on: both the persona/rules block and the per-day
  board JSON are marked `cache_control: { type: "ephemeral" }`. The first
  call in a session pays full price; follow-ups in the same ~5 minutes
  reuse both blocks and cost roughly 90% less.
- To swap to a higher-quality model later, change the `model` field in
  `ask.js` (e.g. `claude-opus-4-7`) and redeploy.

## Common errors

- `upstream 401` — the API key secret isn't set / is invalid; re-run
  `wrangler secret put ANTHROPIC_API_KEY`.
- `upstream 400` — request shape drift; share the response and I'll patch.
- `CORS error` in the browser — the origin isn't in `ALLOWED_ORIGINS`;
  add it and redeploy.

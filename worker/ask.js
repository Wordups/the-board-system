// Ask The Board — Cloudflare Worker proxy.
//
// Front-end widget POSTs /ask with { question, board, history }; this worker
// calls the Anthropic API and returns { answer } or { error }. The Anthropic
// API key lives ONLY as a Worker secret (`wrangler secret put ANTHROPIC_API_KEY`).
// It never goes near the browser bundle and never gets committed to the repo.

const ALLOWED_ORIGINS = new Set([
  "https://wordups.github.io",         // GitHub Pages production
  "http://localhost:8000",             // python -m http.server local preview
  "http://127.0.0.1:8000",
]);

const PERSONA_AND_RULES = `You are "The Board" analyst — direct, concise, talks in tiers. No fluff. No filler. No emojis.

METHODOLOGY:
- Model score tiers: LOCK = 85%+ hit rate, LIVE = 75–84%, FADE = below 75%.
- Parlay rules: each leg needs >=85% hit rate AND >=1.4x payout multiplier.
- 3PT (three-point) props auto-downgrade by 65 points (high volatility).
- Target a 40–45% parlay hit rate — recommend conservative builds (2-leg unless the math really earns more).
- When building a parlay, name the legs, the line, and the per-leg hit rate, and call out the tier ladder.

HARD GROUNDING RULE (non-negotiable, this is the bar):
- You may ONLY reference numbers, player names, lines, scores, sim probabilities, and matchups that appear in the BOARD DATA section below.
- If the user asks about a stat, player, or game that isn't in the board data, respond exactly: "I don't have that in today's board."
- NEVER invent, estimate, round, or extrapolate stats. Made-up numbers are a failure.
- Cite player names and lines verbatim as they appear in the board.
- If a recommendation cannot be supported by the board, say so — don't paper over with a guess.

OUTPUT STYLE:
- Short paragraphs. Bullets when listing picks. Bold the tier label (LOCK / LIVE / FADE).
- Don't narrate your reasoning — just the take and the picks.`;

function corsHeaders(req) {
  const origin = req.headers.get("Origin") || "";
  const allowed = ALLOWED_ORIGINS.has(origin);
  return {
    "Access-Control-Allow-Origin": allowed ? origin : "https://wordups.github.io",
    "Vary": "Origin",
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Max-Age": "86400",
  };
}

function jsonResp(obj, status, req) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { ...corsHeaders(req), "Content-Type": "application/json" },
  });
}

// Two cache breakpoints: the stable persona/rules block and the per-day board
// JSON. Both ephemeral (5-min TTL). Follow-up questions in a chat session hit
// the cache and run roughly 90% cheaper than the first call.
function buildSystemBlocks(board) {
  const dateLabel = board && typeof board === "object" ? (board.date || "today") : "today";
  return [
    { type: "text", text: PERSONA_AND_RULES, cache_control: { type: "ephemeral" } },
    {
      type: "text",
      text:
        `TODAY'S BOARD (${dateLabel}) — this is the ONLY data you may cite:\n` +
        "```json\n" + JSON.stringify(board ?? {}, null, 2) + "\n```",
      cache_control: { type: "ephemeral" },
    },
  ];
}

function buildMessages(history, question) {
  const out = [];
  const turns = Array.isArray(history) ? history.slice(-6) : [];
  for (const turn of turns) {
    if (!turn || typeof turn !== "object") continue;
    const role = turn.role === "assistant" ? "assistant" : "user";
    const content = String(turn.content ?? "").trim();
    if (content) out.push({ role, content });
  }
  out.push({ role: "user", content: String(question) });
  return out;
}

export default {
  async fetch(request, env, ctx) {
    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: corsHeaders(request) });
    }
    const url = new URL(request.url);
    if (url.pathname !== "/ask" || request.method !== "POST") {
      return jsonResp({ error: "POST /ask only" }, 404, request);
    }
    if (!env.ANTHROPIC_API_KEY) {
      return jsonResp(
        { error: "server misconfigured: ANTHROPIC_API_KEY not set (run `wrangler secret put ANTHROPIC_API_KEY`)" },
        500,
        request,
      );
    }

    let body;
    try {
      body = await request.json();
    } catch {
      return jsonResp({ error: "invalid JSON body" }, 400, request);
    }
    const question = String(body?.question ?? "").trim();
    if (!question) return jsonResp({ error: "question required" }, 400, request);
    const board = body?.board ?? null;
    const history = body?.history ?? [];

    try {
      const upstream = await fetch("https://api.anthropic.com/v1/messages", {
        method: "POST",
        headers: {
          "x-api-key": env.ANTHROPIC_API_KEY,
          "anthropic-version": "2023-06-01",
          "content-type": "application/json",
        },
        body: JSON.stringify({
          model: "claude-sonnet-4-6",
          max_tokens: 1024,
          system: buildSystemBlocks(board),
          messages: buildMessages(history, question),
        }),
      });
      if (!upstream.ok) {
        const errText = await upstream.text();
        return jsonResp(
          { error: `upstream ${upstream.status}: ${errText.slice(0, 400)}` },
          502,
          request,
        );
      }
      const data = await upstream.json();
      const blocks = Array.isArray(data?.content) ? data.content : [];
      const answer = blocks
        .filter((b) => b && b.type === "text" && typeof b.text === "string")
        .map((b) => b.text)
        .join("\n")
        .trim();
      return jsonResp({ answer: answer || "(empty response)" }, 200, request);
    } catch (e) {
      return jsonResp({ error: `proxy error: ${e?.message || String(e)}` }, 502, request);
    }
  },
};

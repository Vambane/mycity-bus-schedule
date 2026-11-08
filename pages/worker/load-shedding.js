/**
 * Optional Cloudflare Worker for the national load-shedding stage.
 *
 * The static site never sees ESP_API_KEY. Leave data/ls.json endpoint null
 * and Halte shows "unavailable" while timetables keep working.
 *
 *   wrangler secret put ESP_API_KEY
 *   wrangler deploy
 * Then set dist/data/ls.json "endpoint" to this Worker's https URL and
 * redeploy Pages. Responses are cached for 30 minutes (Cache API, per colo).
 * EskomSePush's free tier is about 50 calls a day, so a busy global cache
 * can still over-call: watch the quota. Do not log the request headers.
 */

const STATUS_URL = "https://developer.sepush.co.za/business/2.0/status";
const TTL_SECONDS = 1800;

function json(body, status, origin) {
  return new Response(JSON.stringify(body), {
    status,
    headers: {
      "content-type": "application/json; charset=utf-8",
      "cache-control": `public, max-age=${TTL_SECONDS}`,
      "access-control-allow-origin": origin,
      "access-control-allow-methods": "GET, OPTIONS",
    },
  });
}

export default {
  async fetch(request, env, ctx) {
    const origin = env.ALLOWED_ORIGIN || "*";
    if (request.method === "OPTIONS") {
      return new Response(null, {
        status: 204,
        headers: {
          "access-control-allow-origin": origin,
          "access-control-allow-methods": "GET, OPTIONS",
          "access-control-max-age": "86400",
        },
      });
    }
    if (request.method !== "GET") return json({available: false, reason: "method"}, 405, origin);

    const cacheKey = new Request(new URL("/v1/stage", request.url).toString(), {method: "GET"});
    const cache = caches.default;
    const hit = await cache.match(cacheKey);
    if (hit) return hit;

    let response;
    if (!env.ESP_API_KEY) {
      response = json({available: false, reason: "not_configured"}, 200, origin);
    } else {
      try {
        const upstream = await fetch(STATUS_URL, {
          headers: {token: env.ESP_API_KEY},
          cf: {cacheTtl: 0},
        });
        if (!upstream.ok) throw new Error("upstream");
        const payload = await upstream.json();
        const stage = Number(payload.status.eskom.stage);
        if (!Number.isInteger(stage) || stage < 0 || stage > 8) throw new Error("range");
        response = json({
          available: true,
          stage,
          source: "auto",
          checked_at: new Date().toISOString(),
        }, 200, origin);
      } catch (_err) {
        response = json({available: false, reason: "upstream"}, 200, origin);
      }
    }
    ctx.waitUntil(cache.put(cacheKey, response.clone()));
    return response;
  },
};

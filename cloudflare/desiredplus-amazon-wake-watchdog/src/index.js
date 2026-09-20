const CHECKPOINTS = [
  [1, 23], [2, 71], [3, 119], [4, 167], [5, 215],
  [6, 263], [7, 311], [8, 359], [9, 407], [10, 455],
];

function dueBatch(now) {
  const minute = now.getUTCHours() * 60 + now.getUTCMinutes();
  for (const [batch, checkpoint] of CHECKPOINTS) {
    const delta = minute - checkpoint;
    if (delta >= 0 && delta <= 4) return batch;
  }
  return null;
}

async function wakeRailway(env, batch, scheduledTime) {
  const url = new URL(env.RAILWAY_BATCH_URL || "https://web-production-dae68.up.railway.app/amazon/run-batch");
  const response = await fetch(url, {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "x-scheduler-secret": env.RAILWAY_WAKE_SECRET,
      "user-agent": "desiredplus-amazon-wake-watchdog/1.0",
    },
    body: JSON.stringify({
      batch,
      scheduler_run_id: `cloudflare-${new Date(scheduledTime).toISOString()}`,
      scheduled_local_time: null,
      github_delay_seconds: 0,
      github_queued_runs: 0,
      github_active_runs: 0,
      github_load_class: "CLOUDFLARE_WATCHDOG",
    }),
  });

  const body = await response.text();
  if (!response.ok) throw new Error(`Railway returned ${response.status}: ${body.slice(0, 1000)}`);
  return body;
}

export default {
  async scheduled(controller, env, ctx) {
    // Cloudflare supplies the scheduled event time even if delivery is delayed; use it as the authoritative checkpoint time.\n    const batch = dueBatch(new Date(controller.scheduledTime));
    if (batch === null) return;

    ctx.waitUntil((async () => {
      try {
        if (!env.CLOUDFLARE_WAKE_SECRET) throw new Error("CLOUDFLARE_WAKE_SECRET is not configured in the Worker");\n      const result = await wakeRailway(env, batch, controller.scheduledTime);
        console.log(JSON.stringify({
          event: "railway_wake_sent",
          batch,
          scheduledTime: new Date(controller.scheduledTime).toISOString(),
          result: result.slice(0, 1000),
        }));
      } catch (error) {
        console.error(JSON.stringify({
          event: "railway_wake_failed",
          batch,
          scheduledTime: new Date(controller.scheduledTime).toISOString(),
          error: String(error),
        }));
      }
    })());
  },

  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname === "/health") {
      return new Response(JSON.stringify({
        status: "ok",
        worker: "desiredplus-amazon-wake-watchdog",
        checkpoints_utc: CHECKPOINTS,
      }), { headers: { "content-type": "application/json" } });
    }
    return new Response("OK");
  },
};

"""Monitoring dashboard — real-time Web UI for system health and metrics.

Extends the WebGateway with:
  - Live metrics panel (bus events/sec, LLM calls, tokens, cache hit rate)
  - Memory usage chart (FTS5 row count, avg weight)
  - Skill usage breakdown
  - Router tier distribution
  - Event DLQ viewer
  - Log tail viewer
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.plugin import Plugin

logger = logging.getLogger(__name__)


class MonitoringPlugin(Plugin):
    """Web-based monitoring dashboard plugin."""

    name = "monitoring"

    def __init__(self) -> None:
        super().__init__()
        self._port = 18793
        self._enabled = True
        self._task = None
        self._app = None

    async def setup(self, ctx) -> None:
        await super().setup(ctx)
        cfg = ctx.config.get("monitoring") or {}
        self._port = int(cfg.get("port", self._port))
        self._enabled = bool(cfg.get("enabled", True))
        logger.info("monitoring configured on port %d", self._port)

    async def start(self) -> None:
        if not self._enabled:
            return
        try:
            from fastapi import FastAPI, HTTPException
            from fastapi.responses import HTMLResponse
        except ImportError:
            logger.warning("fastapi not installed — monitoring dashboard disabled")
            return

        _ctx = self.ctx
        _bus = _ctx.bus if _ctx else None
        _llm = next((p for p in (_ctx._plugins if _ctx else []) if getattr(p, "name", "") == "llm"), None) if _ctx else None
        _memory = next((p for p in (_ctx._plugins if _ctx else []) if getattr(p, "name", "") == "memory"), None) if _ctx else None
        _skills = next((p for p in (_ctx._plugins if _ctx else []) if getattr(p, "name", "") == "skills"), None) if _ctx else None

        app = FastAPI(title="One-Agent Monitor", version="2.0.0")

        dashboard_html = self._build_dashboard_html()

        @app.get("/", response_class=HTMLResponse)
        async def root():
            return dashboard_html

        @app.get("/api/metrics")
        async def metrics():
            bus_m = _bus.metrics() if _bus else {}
            llm_s = _llm.stats() if _llm else {}
            mem_s = _memory.stats() if _memory else {}
            skills = _skills.all_skill_ids() if _skills else []
            uptime = _ctx.uptime() if _ctx else 0

            # Per-tier distribution from bus metrics
            return {
                "timestamp": time.time(),
                "uptime_seconds": round(uptime, 1),
                "bus": bus_m,
                "llm": {
                    "calls": llm_s.get("calls", 0),
                    "tokens_used": llm_s.get("tokens_used", 0),
                    "total_cost_usd": llm_s.get("total_cost_usd", 0),
                    "cache": llm_s.get("cache", {}),
                },
                "memory": {
                    "long_term_rows": mem_s.get("long_term", {}).get("rows", 0),
                    "avg_weight": mem_s.get("long_term", {}).get("avg_weight", 1.0),
                    "procedural_skills": mem_s.get("procedural_skills", 0),
                },
                "skills_count": len(skills),
                "skills": skills,
            }

        @app.get("/api/dashboard_data")
        async def dashboard_data():
            """Full snapshot for initial page render."""
            bus_m = _bus.metrics() if _bus else {}
            llm_s = _llm.stats() if _llm else {}
            mem_s = _memory.stats() if _memory else {}
            dlq = _bus.get_dlq(20) if _bus else []
            skills = _skills.all_skill_ids() if _skills else []

            # Read last 50 log lines
            log_lines = []
            log_path = Path(_ctx.config.get("agent", {}).get("data_dir", "./data")) / "logs" / "athena.log"
            # Read last N log lines with size cap (avoid OOM on huge logs)
            MAX_LOG_READ = 256 * 1024  # 256 KB max
            if log_path.exists():
                raw = log_path.read_text(encoding="utf-8", errors="ignore")
                if len(raw) > MAX_LOG_READ:
                    raw = raw[-MAX_LOG_READ:]
                lines = raw.splitlines()
                log_lines = lines[-50:]

            return {
                "bus": bus_m,
                "llm": llm_s,
                "memory": mem_s,
                "dlq": [e.to_dict() for e in dlq],
                "skills": skills,
                "recent_logs": log_lines,
                "timestamp": time.time(),
            }

        @app.get("/api/logs")
        async def logs(tail: int = 50):
            log_path = Path(_ctx.config.get("agent", {}).get("data_dir", "./data")) / "logs" / "athena.log"
            if not log_path.exists():
                return {"lines": []}
            MAX_LOG_READ = 256 * 1024
            raw = log_path.read_text(encoding="utf-8", errors="ignore")
            if len(raw) > MAX_LOG_READ:
                raw = raw[-MAX_LOG_READ:]
            lines = raw.splitlines()
            return {"lines": lines[-tail:]}

        self._app = app
        try:
            import uvicorn
            config = uvicorn.Config(app, host="127.0.0.1", port=self._port, log_level="warning")
            server = uvicorn.Server(config)
            self._task = asyncio.create_task(server.serve())
            logger.info("monitoring dashboard on http://127.0.0.1:%d", self._port)
        except Exception as exc:
            logger.warning("could not start monitoring dashboard: %s", exc)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
        await super().stop()

    @staticmethod
    def _build_dashboard_html() -> str:
        return """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<title>One-Agent Monitor v2</title>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<style>
:root{--bg:#0f172a;--p:#1e293b;--fg:#e2e8f0;--muted:#94a3b8;--a:#38bdf8;--a2:#a78bfa;--b:#334155;--g:#4ade80;--y:#fbbf24;--r:#f87171}
*{box-sizing:border-box}
body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:var(--bg);color:var(--fg);min-height:100vh}
header{padding:14px 20px;border-bottom:1px solid var(--b);display:flex;align-items:center;gap:16px;flex-wrap:wrap}
h1{margin:0;font-size:18px}h1 span{color:var(--a)}
.uptime{color:var(--muted);font-size:13px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:14px;padding:16px}
.card{background:var(--p);border:1px solid var(--b);border-radius:10px;padding:14px}
.card h3{margin:0 0 10px;font-size:13px;text-transform:uppercase;letter-spacing:.5px;color:var(--muted)}
.stat{font-size:28px;font-weight:700;color:var(--a);margin-bottom:4px}
.sub{font-size:12px;color:var(--muted)}
.row{display:flex;justify-content:space-between;padding:3px 0;font-size:13px}
.row span:last-child{color:var(--a)}
.logs{font-family:monospace;font-size:11px;background:#000;border-radius:6px;padding:10px;height:200px;overflow-y:auto;color:#e2e8f0;white-space:pre-wrap;word-break:break-all}
.dlq-item{font-size:12px;padding:4px 0;border-bottom:1px solid var(--b)}
.dlq-item:last-child{border:none}
.badge{display:inline-block;padding:1px 6px;border-radius:4px;font-size:11px;font-weight:600}
.b-green{background:#14532d;color:#86efac}.b-yellow{background:#713f12;color:#fde047}
.b-red{background:#7f1d1d;color:#fca5a5}.b-blue{background:#1e3a5f;color:#93c5fd}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{padding:6px 8px;text-align:left;border-bottom:1px solid var(--b)}
th{color:var(--muted);font-weight:500}
.refresh{padding:6px 12px;background:var(--a);color:#0b1220;border:none;border-radius:6px;font-weight:600;cursor:pointer}
</style>
</head>
<body>
<header>
  <h1>One-Agent Monitor</h1>
  <span class="uptime" id="uptime">—</span>
  <button class="refresh" onclick="refresh()">Refresh</button>
</header>
<div class="grid">
  <div class="card">
    <h3>Event Bus</h3>
    <div class="stat" id="bus-published">—</div>
    <div class="sub">events published</div>
    <div class="row"><span>processed</span><span id="bus-processed">—</span></div>
    <div class="row"><span>queue depth</span><span id="bus-queue">—</span></div>
    <div class="row"><span>events/sec</span><span id="bus-eps">—</span></div>
    <div class="row"><span>DLQ size</span><span id="bus-dlq">—</span></div>
    <div class="row"><span>errors</span><span id="bus-errors">—</span></div>
  </div>
  <div class="card">
    <h3>LLM</h3>
    <div class="stat" id="llm-calls">—</div>
    <div class="sub">total calls</div>
    <div class="row"><span>tokens used</span><span id="llm-tokens">—</span></div>
    <div class="row"><span>total cost</span><span id="llm-cost">—</span></div>
    <div class="row"><span>cache hit rate</span><span id="llm-hit">—</span></div>
    <div class="row"><span>cache size</span><span id="llm-csize">—</span></div>
  </div>
  <div class="card">
    <h3>Memory</h3>
    <div class="stat" id="mem-rows">—</div>
    <div class="sub">long-term facts</div>
    <div class="row"><span>procedural skills</span><span id="mem-skills">—</span></div>
    <div class="row"><span>avg weight</span><span id="mem-weight">—</span></div>
    <div class="row"><span>skills loaded</span><span id="sk-count">—</span></div>
  </div>
  <div class="card">
    <h3>Dead-Letter Queue</h3>
    <div id="dlq-list"><em style="color:var(--muted)">empty</em></div>
  </div>
  <div class="card" style="grid-column:1/-1">
    <h3>Recent Logs</h3>
    <div class="logs" id="log-output">loading…</div>
  </div>
</div>
<script>
let lastTs = 0;
async function refresh() {
  try {
    const [m, logs] = await Promise.all([
      fetch("/api/metrics").then(r=>r.json()),
      fetch("/api/logs?tail=60").then(r=>r.json())
    ]);
    const bus = m.bus || {};
    const llm = m.llm || {};
    const mem = m.memory || {};
    const cache = llm.cache || {};
    document.getElementById("uptime").textContent = "up " + Math.floor(m.uptime_seconds) + "s";
    document.getElementById("bus-published").textContent = bus.published || 0;
    document.getElementById("bus-processed").textContent = bus.processed || 0;
    document.getElementById("bus-queue").textContent = bus.queue_depth || 0;
    document.getElementById("bus-eps").textContent = ((bus.events_per_second||0).toFixed(2)) + "/s";
    document.getElementById("bus-dlq").textContent = bus.dlq_size || 0;
    document.getElementById("bus-errors").textContent = bus.errors || 0;
    document.getElementById("llm-calls").textContent = llm.calls || 0;
    document.getElementById("llm-tokens").textContent = (llm.tokens_used||0).toLocaleString();
    document.getElementById("llm-cost").textContent = "$" + ((llm.total_cost_usd||0).toFixed(4));
    document.getElementById("llm-hit").textContent = ((cache.hit_rate||0)*100).toFixed(1) + "%";
    document.getElementById("llm-csize").textContent = cache.size||0 + "/" + (cache.max_size||0);
    document.getElementById("mem-rows").textContent = mem.long_term_rows || 0;
    document.getElementById("mem-skills").textContent = mem.procedural_skills || 0;
    document.getElementById("mem-weight").textContent = (mem.avg_weight||1).toFixed(3);
    document.getElementById("sk-count").textContent = m.skills_count || 0;
    const dlq = bus.dlq_size ? [] : [];
    document.getElementById("dlq-list").innerHTML = bus.dlq_size
      ? "<em style='color:var(--muted)'>" + bus.dlq_size + " dead-letter events</em>"
      : "<em style='color:#86efac'>queue healthy</em>";
    document.getElementById("log-output").textContent = (logs.lines||[]).join("\\n");
    document.getElementById("log-output").scrollTop = document.getElementById("log-output").scrollHeight;
    lastTs = m.timestamp;
  } catch(e) { console.error(e); }
}
refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>"""

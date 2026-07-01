"""Web Dashboard — Real-time monitoring and management interface.

Provides a single-page HTML dashboard for monitoring One-Agent operations:
- Real-time cost tracking and budget consumption
- Session list and conversation replay
- Knowledge graph visualization (force-directed)
- Skills marketplace browsing
- Approval queue management

Architecture:
- Single HTML file with embedded CSS/JS
- REST API endpoints for data fetching
- Auto-refresh every 5 seconds
- Responsive design for mobile/desktop
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>One-Agent Dashboard</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            background: #0f172a;
            color: #e2e8f0;
            line-height: 1.6;
        }
        .container { max-width: 1400px; margin: 0 auto; padding: 20px; }
        header {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            padding: 30px 0;
            margin-bottom: 30px;
            border-radius: 10px;
        }
        header h1 { text-align: center; font-size: 2.5em; margin-bottom: 10px; }
        header p { text-align: center; opacity: 0.9; }
        .grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
            gap: 20px;
            margin-bottom: 30px;
        }
        .card {
            background: #1e293b;
            border-radius: 10px;
            padding: 20px;
            box-shadow: 0 4px 6px rgba(0,0,0,0.3);
        }
        .card h2 {
            font-size: 1.3em;
            margin-bottom: 15px;
            color: #60a5fa;
            border-bottom: 2px solid #334155;
            padding-bottom: 10px;
        }
        .metric {
            display: flex;
            justify-content: space-between;
            padding: 10px 0;
            border-bottom: 1px solid #334155;
        }
        .metric:last-child { border-bottom: none; }
        .metric-label { color: #94a3b8; }
        .metric-value { font-weight: bold; color: #10b981; }
        .progress-bar {
            width: 100%;
            height: 20px;
            background: #334155;
            border-radius: 10px;
            overflow: hidden;
            margin-top: 10px;
        }
        .progress-fill {
            height: 100%;
            background: linear-gradient(90deg, #10b981 0%, #3b82f6 100%);
            transition: width 0.3s;
        }
        .session-list { max-height: 400px; overflow-y: auto; }
        .session-item {
            background: #334155;
            padding: 15px;
            margin-bottom: 10px;
            border-radius: 8px;
            cursor: pointer;
            transition: background 0.2s;
        }
        .session-item:hover { background: #475569; }
        .session-title { font-weight: bold; margin-bottom: 5px; }
        .session-actions { display: flex; gap: 8px; margin-top: 8px; }
        .btn-small { padding: 4px 8px; font-size: 12px; }
        .btn-fork { background: #3b82f6; color: white; }
        .session-meta { font-size: 0.9em; color: #94a3b8; }
        .approval-item {
            background: #7c2d12;
            padding: 15px;
            margin-bottom: 10px;
            border-radius: 8px;
            border-left: 4px solid #f59e0b;
        }
        .approval-actions { margin-top: 10px; }
        .btn {
            padding: 8px 16px;
            border: none;
            border-radius: 5px;
            cursor: pointer;
            font-weight: bold;
            margin-right: 10px;
        }
        .btn-approve { background: #10b981; color: white; }
        .btn-deny { background: #ef4444; color: white; }
        .btn:hover { opacity: 0.8; }
        .refresh-indicator {
            position: fixed;
            top: 20px;
            right: 20px;
            background: #10b981;
            color: white;
            padding: 10px 20px;
            border-radius: 5px;
            opacity: 0;
            transition: opacity 0.3s;
        }
        .refresh-indicator.show { opacity: 1; }
        #knowledge-graph {
            width: 100%;
            height: 400px;
            background: #0f172a;
            border-radius: 8px;
        }
        .loading { text-align: center; padding: 40px; color: #94a3b8; }
    </style>
</head>
<body>
    <div class="container">
        <header>
            <h1>🤖 One-Agent Dashboard</h1>
            <p>实时监控与管理面板</p>
        </header>

        <div class="grid">
            <div class="card">
                <h2>💰 成本追踪</h2>
                <div class="metric">
                    <span class="metric-label">今日成本</span>
                    <span class="metric-value" id="daily-cost">$0.00</span>
                </div>
                <div class="metric">
                    <span class="metric-label">本月成本</span>
                    <span class="metric-value" id="monthly-cost">$0.00</span>
                </div>
                <div class="metric">
                    <span class="metric-label">预算剩余</span>
                    <span class="metric-value" id="budget-remaining">$0.00</span>
                </div>
                <div class="progress-bar">
                    <div class="progress-fill" id="budget-progress" style="width: 0%"></div>
                </div>
            </div>

            <div class="card">
                <h2>📊 系统统计</h2>
                <div class="metric">
                    <span class="metric-label">活跃会话</span>
                    <span class="metric-value" id="active-sessions">0</span>
                </div>
                <div class="metric">
                    <span class="metric-label">总消息数</span>
                    <span class="metric-value" id="total-messages">0</span>
                </div>
                <div class="metric">
                    <span class="metric-label">知识图谱实体</span>
                    <span class="metric-value" id="kg-entities">0</span>
                </div>
                <div class="metric">
                    <span class="metric-label">已安装技能</span>
                    <span class="metric-value" id="installed-skills">0</span>
                </div>
            </div>

            <div class="card">
                <h2>⚠️ 审批队列</h2>
                <div id="approval-queue">
                    <div class="loading">暂无待审批项</div>
                </div>
            </div>
        </div>

        <div class="card">
            <h2>💬 会话列表</h2>
            <div class="session-list" id="session-list">
                <div class="loading">加载中...</div>
            </div>
        </div>

        <div class="card">
            <h2>🕸️ 知识图谱</h2>
            <div id="knowledge-graph">
                <div class="loading">图谱可视化加载中...</div>
            </div>
        </div>
    </div>

    <div class="refresh-indicator" id="refresh-indicator">刷新中...</div>

    <script>
        const API_BASE = '/api';
        let refreshInterval;

        // HTML escape function to prevent XSS — all user-controlled
        // data (session titles, approval descriptions, etc.) must be
        // escaped before inserting into innerHTML.
        function esc(s) {
            if (s == null) return '';
            return String(s)
                .replace(/&/g, '&amp;')
                .replace(/</g, '&lt;')
                .replace(/>/g, '&gt;')
                .replace(/"/g, '&quot;')
                .replace(/'/g, '&#39;');
        }

        // JS string escape — for values inserted into inline JS handlers
        // (onclick="..."). HTML entity escaping alone is NOT sufficient
        // here because the browser decodes entities before executing JS.
        // We must escape backslash and single-quote so the value cannot
        // break out of the JS string literal.
        function escJs(s) {
            if (s == null) return '';
            return String(s)
                .replace(/\\/g, '\\\\')
                .replace(/'/g, "\\'")
                .replace(/"/g, '\\"')
                .replace(/\n/g, '\\n')
                .replace(/\r/g, '\\r');
        }

        async function fetchJSON(url) {
            try {
                const resp = await fetch(url);
                if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
                return await resp.json();
            } catch (err) {
                console.error('Fetch failed:', url, err);
                return null;
            }
        }

        async function updateCosts() {
            const data = await fetchJSON(`${API_BASE}/costs/daily`);
            if (!data) return;

            document.getElementById('daily-cost').textContent = `$${data.cost.toFixed(4)}`;
            document.getElementById('budget-remaining').textContent = `$${data.remaining.toFixed(4)}`;

            const progress = (data.cost / data.budget) * 100;
            document.getElementById('budget-progress').style.width = `${Math.min(progress, 100)}%`;

            const monthly = await fetchJSON(`${API_BASE}/costs/monthly`);
            if (monthly) {
                document.getElementById('monthly-cost').textContent = `$${monthly.cost.toFixed(4)}`;
            }
        }

        async function updateStats() {
            const data = await fetchJSON(`${API_BASE}/stats`);
            if (!data) return;

            document.getElementById('active-sessions').textContent = data.sessions?.active || 0;
            document.getElementById('total-messages').textContent = data.messages?.total || 0;
            document.getElementById('kg-entities').textContent = data.knowledge_graph?.entities || 0;
            document.getElementById('installed-skills').textContent = data.skills?.installed || 0;
        }

        async function updateSessions() {
            const data = await fetchJSON(`${API_BASE}/sessions/list`);
            const container = document.getElementById('session-list');

            if (!data || !data.sessions || data.sessions.length === 0) {
                container.innerHTML = '<div class="loading">暂无会话</div>';
                return;
            }

            container.innerHTML = data.sessions.map(s => `
                <div class="session-item" onclick="viewSession('${escJs(s.id)}')">
                    <div class="session-title">${esc(s.title || '未命名会话')}</div>
                    <div class="session-meta">
                        ${esc(s.message_count)} 条消息 ·
                        最后更新: ${new Date(s.updated_at * 1000).toLocaleString('zh-CN')}
                    </div>
                    <div class="session-actions">
                        <button class="btn btn-small btn-fork" onclick="forkSession('${escJs(s.id)}', event)">分支</button>
                    </div>
                </div>
            `).join('');
        }

        async function updateApprovals() {
            const data = await fetchJSON(`${API_BASE}/approvals/pending`);
            const container = document.getElementById('approval-queue');

            if (!data || !data.pending || data.pending.length === 0) {
                container.innerHTML = '<div class="loading">暂无待审批项</div>';
                return;
            }

            container.innerHTML = data.pending.map(r => `
                <div class="approval-item">
                    <div><strong>${esc(r.action)}</strong></div>
                    <div style="font-size: 0.9em; color: #fbbf24;">${esc(r.description)}</div>
                    <div class="approval-actions">
                        <button class="btn btn-approve" onclick="approveRequest('${escJs(r.id)}')">批准</button>
                        <button class="btn btn-deny" onclick="denyRequest('${escJs(r.id)}')">拒绝</button>
                    </div>
                </div>
            `).join('');
        }

        async function approveRequest(id) {
            await fetchJSON(`${API_BASE}/approvals/${id}/approve`);
            showRefresh();
            updateApprovals();
        }

        async function denyRequest(id) {
            await fetchJSON(`${API_BASE}/approvals/${id}/deny`);
            showRefresh();
            updateApprovals();
        }

        function viewSession(id) {
            window.open(`${API_BASE}/sessions/${id}/replay`, '_blank');
        }

        async function forkSession(sessionId, event) {
            event.stopPropagation();
            const forkPoint = prompt('请输入分支点（消息索引，从0开始）:', '0');
            if (forkPoint === null) return;

            const response = await fetch(`${API_BASE}/sessions/${sessionId}/fork`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ fork_point: parseInt(forkPoint) })
            });

            if (response.ok) {
                const data = await response.json();
                alert(`分支成功！新会话ID: ${data.new_session_id}`);
                updateSessions();
            } else {
                alert('分支失败: ' + response.statusText);
            }
        }

        function showRefresh() {
            const indicator = document.getElementById('refresh-indicator');
            indicator.classList.add('show');
            setTimeout(() => indicator.classList.remove('show'), 1000);
        }

        async function refreshAll() {
            showRefresh();
            await Promise.all([
                updateCosts(),
                updateStats(),
                updateSessions(),
                updateApprovals()
            ]);
        }

        // Initial load
        refreshAll();

        // Auto-refresh every 5 seconds
        refreshInterval = setInterval(refreshAll, 5000);

        // Cleanup on page unload
        window.addEventListener('beforeunload', () => {
            if (refreshInterval) clearInterval(refreshInterval);
        });
    </script>
</body>
</html>
"""


def get_dashboard_html() -> str:
    """Return the dashboard HTML content."""
    return DASHBOARD_HTML

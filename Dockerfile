# ============================================================
# One-Agent Production Dockerfile
# ============================================================

FROM python:3.12-slim

# --- System dependencies ---
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# --- Application directory ---
WORKDIR /app

# --- Layer 1: Dependencies (cached unless requirements.txt changes) ---
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# --- Layer 2: Application source ---
COPY . .

# --- Expose ports ---
# 18791: Web Gateway (REST API / JSON)
# 18792: REST API
# 18793: Monitor
EXPOSE 18791 18792 18793

# --- Data volume (v2.2.0) ---
# 统一数据库 {data_dir}/one_agent.db 承载全部系统状态（配置/会话/记忆/
# 审批/凭据/技能包）。必须声明为卷，否则容器重建即丢失全部数据，
# 与"复制一个数据库即可迁移环境"的架构目标直接冲突。
# 挂载示例: docker run -v one-agent-data:/app/data ...
VOLUME /app/data

# --- Config path ---
ENV ONE_AGENT_CONFIG=/app/config/default_config.yaml \
    ONE_AGENT_DATA_DIR=/app/data

# --- Security: non-root user ---
RUN groupadd -r agentgroup && \
    useradd -r -g agentgroup -s /sbin/nologin -d /app agentuser && \
    chown -R agentuser:agentgroup /app

USER agentuser

# --- Start ---
CMD ["python", "one_agent.py"]
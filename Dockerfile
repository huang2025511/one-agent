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
# 18791: Web UI
# 18792: REST API
# 18793: Monitor
EXPOSE 18791 18792 18793

# --- Config path ---
ENV ONE_AGENT_CONFIG=/app/config/default_config.yaml

# --- Security: non-root user ---
RUN groupadd -r agentgroup && \
    useradd -r -g agentgroup -s /sbin/nologin -d /app agentuser && \
    chown -R agentuser:agentgroup /app

USER agentuser

# --- Start ---
CMD ["python", "one_agent.py"]
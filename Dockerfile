# Stage 1: Build Python dependencies
FROM python:3.11-slim AS builder

# Install uv for fast dependency resolution
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml README.md ./
COPY pyclaudir/ pyclaudir/

# Create venv and install production dependencies (not dev/test)
RUN uv venv /app/.venv && \
    uv pip install --python /app/.venv/bin/python . --no-cache-dir

# Stage 2: Runtime
FROM python:3.11-slim

# Install Node.js (needed for Claude Code CLI + npx for GitLab MCP) and
# tini (a minimal init that reaps zombies — important since render_html
# spawns Chromium as a subprocess; if Chromium gets orphaned we don't
# want it lingering as a zombie under python-as-PID-1).
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl ca-certificates tini && \
    curl -fsSL https://deb.nodesource.com/setup_22.x | bash - && \
    apt-get install -y --no-install-recommends nodejs && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# Install Claude Code CLI
RUN npm install -g @anthropic-ai/claude-code

# Install uv (needed for uvx / mcp-atlassian)
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Pre-install mcp-atlassian so it's available at runtime
RUN uv tool install mcp-atlassian
ENV PATH="/root/.local/bin:$PATH"

# Copy Python venv from builder
COPY --from=builder /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# Headless Chromium for the render_html tool. Playwright bundles its own
# browser binary under /root/.cache/ms-playwright; --with-deps installs
# the shared libs Chrome needs (atk, nss, libdrm, etc.).
RUN /app/.venv/bin/playwright install --with-deps chromium

# Copy application source, prompts, and skill playbooks
COPY pyclaudir/ pyclaudir/
COPY prompts/system.md prompts/system.md
COPY skills/ skills/

# Plugin config — the example is always shipped; a real ``plugins.json``
# is bundled if the operator has copied it before ``docker build``.
# Anchoring the COPY on plugins.json.example (always present) lets the
# trailing ``plugins.json*`` glob be a no-op when the developer hasn't
# run ``cp plugins.json.example plugins.json`` yet, in both classic and
# BuildKit builders.
COPY plugins.json.example plugins.json* ./

# Data directory (mount as volume)
VOLUME /app/data

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "pyclaudir"]

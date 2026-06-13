# syntax=docker/dockerfile:1
FROM python:3.12-slim

WORKDIR /app

# Layer 1: system tools — changes rarely, cached aggressively
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl xz-utils ca-certificates git tor netcat-openbsd \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
       | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
       > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y --no-install-recommends gh \
    && NODE_VERSION=22.16.0 \
    && ARCH=$(dpkg --print-architecture) \
    && NODE_ARCH=$([ "$ARCH" = "amd64" ] && echo "x64" || echo "$ARCH") \
    && curl -fsSL https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-${NODE_ARCH}.tar.xz \
       | tar -xJ -C /usr/local --strip-components=1 \
    && npm install -g @anthropic-ai/claude-code \
    && rm -rf /var/lib/apt/lists/* /root/.npm

# Layer 2: Python deps — invalidated only when requirements.txt changes
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt uv

# Layer 3: code — invalidated on code changes, NOT on prompt-only changes
COPY --exclude=claude_works/prompts . .
RUN chmod +x /app/entrypoint.sh

# Layer 4: prompts — thin layer, only rebuilt when prompts change
COPY claude_works/prompts/ ./claude_works/prompts/

ENV PYTHONUNBUFFERED=1
ENV CLAUDE_HOME=/data/.claude

VOLUME ["/data"]
EXPOSE 8080

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["python", "-m", "claude_works.supervisor"]

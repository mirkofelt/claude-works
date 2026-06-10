FROM python:3.12-slim

WORKDIR /app

# System tools + gh CLI + Tor + Node.js (for claude CLI)
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl xz-utils ca-certificates tor \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
       | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
       > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y --no-install-recommends gh \
    && NODE_VERSION=22.16.0 \
    && curl -fsSL https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-x64.tar.xz \
       | tar -xJ -C /usr/local --strip-components=1 \
    && npm install -g @anthropic-ai/claude-code \
    && rm -rf /var/lib/apt/lists/* /root/.npm

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN chmod +x /app/entrypoint.sh

ENV PYTHONUNBUFFERED=1
ENV CLAUDE_HOME=/data/.claude

VOLUME ["/data"]
EXPOSE 8080

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["python", "-m", "claude_works.supervisor"]

#!/bin/bash
set -e
OLLAMA="http://localhost:11434"

echo "⏳ Waiting for ollama-codereview..."
until curl -sf "$OLLAMA/api/tags" >/dev/null 2>&1; do sleep 2; done
echo "✓ Ollama is up"

for MODEL in "qwen3-coder:latest" "deepseek-r1:32b" "nomic-embed-text:latest"; do
  echo "→ Pulling $MODEL"
  curl -sf "$OLLAMA/api/pull" -d "{\"name\":\"$MODEL\"}" | while read -r line; do
    STATUS=$(echo "$line" | grep -oP '"status"\s*:\s*"\K[^"]+' || true)
    [ -n "$STATUS" ] && printf "\r  %s" "$STATUS"
  done
  echo -e "\n  ✓ $MODEL ready"
done

echo -e "\n✓ Done. Open http://$(hostname -I | awk '{print $1}'):8090"

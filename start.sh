export OLLAMA_KEEP_ALIVE=${OLLAMA_KEEP_ALIVE:-6h}
#!/usr/bin/env bash
set -e

ollama serve &
sleep 5

if [ -n "$DEFAULT_MODEL" ]; then
  ollama pull "$DEFAULT_MODEL" || true
fi

python bot.py

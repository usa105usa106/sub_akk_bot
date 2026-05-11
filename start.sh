#!/usr/bin/env bash
set -e

echo "Starting Ollama..."
ollama serve &
OLLAMA_PID=$!

sleep 5

DEFAULT_OLLAMA_MODEL="${DEFAULT_OLLAMA_MODEL:-llama3.1:8b}"

echo "Pulling default Ollama model only: $DEFAULT_OLLAMA_MODEL"
ollama pull "$DEFAULT_OLLAMA_MODEL" || true

echo "Starting Telegram bot..."
python bot.py

wait $OLLAMA_PID

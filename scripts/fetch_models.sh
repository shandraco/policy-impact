#!/usr/bin/env bash
# Downloads the model weights into ./models on a machine with internet. The model containers never download.
# Needs: pip install -U "huggingface_hub[cli]"
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p models
for m in openai/gpt-oss-120b openai/gpt-oss-20b nomic-ai/nomic-embed-text-v1.5; do
  huggingface-cli download "$m" --local-dir "models/${m#*/}"
done
echo "Done. Models are in ./models"

#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
LOCAL_ENV_FILE="${SCRIPT_DIR}/local_env.sh"

if [ -f "${LOCAL_ENV_FILE}" ]; then
    # Load untracked local secrets and proxy settings.
    # shellcheck disable=SC1090
    source "${LOCAL_ENV_FILE}"
fi

AI_BACKEND=local
AI_PROVIDER=openai
CODEX_PROXY_BASE_URL="${CODEX_PROXY_BASE_URL:-http://127.0.0.1:8787/v1}"
CODEX_PROXY_URL="${CODEX_PROXY_URL:-${CODEX_PROXY_BASE_URL%/}/chat/completions}"
CODEX_PROXY_BASE_URL="${CODEX_PROXY_URL%/chat/completions}"
OPENAI_URL="${CODEX_PROXY_URL}"
OPENAI_BASE_URL="${CODEX_PROXY_BASE_URL}"
OPENAI_API_KEY="${CODEX_PROXY_API_KEY:-codex-proxy}"
OPENAI_MODEL=gpt-6-astra
AI_BASE_URL="${OPENAI_URL}"
AI_API_KEY="${OPENAI_API_KEY}"
AI_MODEL="${OPENAI_MODEL}"

if [ -n "${LOCAL_HTTP_PROXY:-}" ]; then
    export HTTP_PROXY="${HTTP_PROXY:-${LOCAL_HTTP_PROXY}}"
    export HTTPS_PROXY="${HTTPS_PROXY:-${LOCAL_HTTP_PROXY}}"
    export http_proxy="${http_proxy:-${LOCAL_HTTP_PROXY}}"
    export https_proxy="${https_proxy:-${LOCAL_HTTP_PROXY}}"
fi

export AI_BACKEND AI_PROVIDER AI_BASE_URL AI_API_KEY AI_MODEL
export OPENAI_URL OPENAI_BASE_URL OPENAI_API_KEY OPENAI_MODEL
export CODEX_PROXY_URL CODEX_PROXY_BASE_URL

cd "${PROJECT_ROOT}"
exec python3 "${SCRIPT_DIR}/download_paper.py" \
    --extractor hybrid \
    --timeout "${DOWNLOAD_PAPER_TIMEOUT:-1800}" \
    --workers "${DOWNLOAD_PAPER_WORKERS:-1}" \
    --google-scholar-cdp-url "${GOOGLE_SCHOLAR_CDP_URL:-http://127.0.0.1:9222}" \
    "$@"

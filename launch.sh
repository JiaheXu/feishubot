#!/usr/bin/env bash
set -euo pipefail
umask 077

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
LOCAL_ENV_FILE="${SCRIPT_DIR}/local_env.sh"

if [ -f "${LOCAL_ENV_FILE}" ]; then
    # Export local settings for Python without printing their values.
    set -a
    # shellcheck disable=SC1090
    source "${LOCAL_ENV_FILE}"
    set +a
fi

# Match the OpenAI-compatible proxy configuration in download_paper.sh.
CODEX_PROXY_BASE_URL="${CODEX_PROXY_BASE_URL:-http://127.0.0.1:8787/v1}"
CODEX_PROXY_URL="${CODEX_PROXY_URL:-${CODEX_PROXY_BASE_URL%/}/chat/completions}"
AI_BASE_URL="${AI_BASE_URL:-${OPENAI_URL:-${CODEX_PROXY_URL}}}"
AI_API_KEY="${AI_API_KEY:-${CODEX_PROXY_API_KEY:-${OPENAI_API_KEY:-codex-proxy}}}"
AI_MODEL="${AI_MODEL:-${OPENAI_MODEL:-gpt-6-astra}}"
export CODEX_PROXY_BASE_URL CODEX_PROXY_URL AI_BASE_URL AI_API_KEY AI_MODEL

if [ -n "${LOCAL_HTTP_PROXY:-}" ]; then
    export HTTP_PROXY="${HTTP_PROXY:-${LOCAL_HTTP_PROXY}}"
    export HTTPS_PROXY="${HTTPS_PROXY:-${LOCAL_HTTP_PROXY}}"
    export http_proxy="${http_proxy:-${LOCAL_HTTP_PROXY}}"
    export https_proxy="${https_proxy:-${LOCAL_HTTP_PROXY}}"
fi
export NO_PROXY="${NO_PROXY:+${NO_PROXY},}127.0.0.1,localhost,::1"
export no_proxy="${no_proxy:+${no_proxy},}127.0.0.1,localhost,::1"

if [ -z "${PYTHON_BIN:-}" ]; then
    if [ -x "${SCRIPT_DIR}/.venv/bin/python" ]; then
        PYTHON_BIN="${SCRIPT_DIR}/.venv/bin/python"
    else
        PYTHON_BIN=python3
    fi
fi

cd "${SCRIPT_DIR}"
exec "${PYTHON_BIN}" "${SCRIPT_DIR}/monitor.py" "$@"

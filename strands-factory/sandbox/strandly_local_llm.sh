#!/usr/bin/env bash
# Runs COMMAND against local Ollama (and optionally llama-server), stopping both when it exits.
# Sandbox commands do not keep background processes, so servers and tests share one invocation.
set -euo pipefail

usage="usage: local-llm [--model NAME] [--alias NAME] [--llama-server] -- COMMAND [ARGS...]"
model=qwen2.5:3b alias="" llama_server=""
while (($#)); do
  case $1 in
    --model) model=$2; shift 2 ;;
    --alias) alias=$2; shift 2 ;;
    --llama-server) llama_server=1; shift ;;
    --) shift; break ;;
    *) echo "$usage" >&2; exit 2 ;;
  esac
done
(($#)) || { echo "$usage" >&2; exit 2; }

# Not /mnt/workspace: session storage is capped at 1 GB.
state=${LOCAL_LLM_DIR:-/tmp/local-llm}
threads=2
mkdir -p "$state"
export OLLAMA_HOST=127.0.0.1:11434 OLLAMA_MODELS=$state/models OLLAMA_NO_CLOUD=1

pids=()
cleanup() {
  kill "${pids[@]}" 2>/dev/null || true
  wait "${pids[@]}" 2>/dev/null || true
}
trap cleanup EXIT

wait_ready() {
  for _ in $(seq 300); do
    curl -sf -o /dev/null "$1" && return
    kill -0 "${pids[-1]}" 2>/dev/null || break
    sleep 1
  done
  echo "local-llm: $1 did not come up" >&2
  tail -n 20 "$2" >&2
  exit 1
}

quiet() {
  "$@" >"$state/cmd.log" 2>&1 || { tail -c 500 "$state/cmd.log" >&2; return 1; }
}

ollama serve >"$state/ollama.log" 2>&1 &
pids+=($!)
wait_ready http://127.0.0.1:11434/ "$state/ollama.log"
quiet ollama pull "$model"

if [[ -n $alias ]]; then
  printf 'FROM %s\nPARAMETER num_thread %s\n' "$model" "$threads" >"$state/Modelfile"
  quiet ollama create "$alias" -f "$state/Modelfile"
fi

if [[ -n $llama_server ]]; then
  gguf=$(ollama show --modelfile "$model" | awk '$1 == "FROM" { print $2; exit }')
  /opt/ollama/lib/ollama/llama-server -m "$gguf" --host 127.0.0.1 --port 8081 \
    -t "$threads" -c 4096 -np 1 --no-webui >"$state/llama-server.log" 2>&1 &
  pids+=($!)
  wait_ready http://127.0.0.1:8081/health "$state/llama-server.log"
fi

"$@"

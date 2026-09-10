#!/usr/bin/env bash
# One-command install: Qwen (llama.cpp) + Hearth chat/agent + Aider.
# Run on the Jetson:  ./install.sh
# Or: curl -fsSL https://raw.githubusercontent.com/cfollette18/hearth/main/install.sh | bash
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]:-.}")" && pwd)"
if [ ! -f "$SRC/hearth/server.py" ]; then
  echo ">> cloning hearth"
  git clone --depth 1 https://github.com/cfollette18/hearth.git "$HOME/.local/src/hearth"
  SRC="$HOME/.local/src/hearth"
fi

PREFIX="${HEARTH_PREFIX:-$HOME/.local/share/hearth}"
BIN="$HOME/.local/bin"
LLAMA_DIR="${HEARTH_LLAMA:-$HOME/llama.cpp}"
MODEL_DIR="${HEARTH_MODELS:-$HOME/models}"
MODEL_FILE="${HEARTH_MODEL_FILE:-Qwen3.5-4B-Uncensored-Q4_K_M.gguf}"
MODEL_URL="${HEARTH_MODEL_URL:-https://huggingface.co/HauhauCS/Qwen3.5-4B-Uncensored-GGUF/resolve/main/${MODEL_FILE}}"
WORKSPACE="${HEARTH_WORKSPACE:-$HOME/hearth-workspace}"
PORT="${HEARTH_PORT:-8090}"
LLAMA_PORT="${HEARTH_LLAMA_PORT:-8082}"

mkdir -p "$PREFIX" "$BIN" "$MODEL_DIR" "$WORKSPACE" "$HOME/.config/systemd/user" "$HOME/.config/hearth"

echo ">> installing hearth to $PREFIX"
cp -a "$SRC/hearth" "$PREFIX/"
cp -a "$SRC/pack" "$PREFIX/" 2>/dev/null || true

if [ ! -x "$LLAMA_DIR/build/bin/llama-server" ]; then
  echo ">> building llama.cpp with CUDA (this can take a while)"
  if [ ! -d "$LLAMA_DIR" ]; then
    git clone https://github.com/ggml-org/llama.cpp "$LLAMA_DIR"
  fi
  export PATH="/usr/local/cuda/bin:$PATH"
  cmake -S "$LLAMA_DIR" -B "$LLAMA_DIR/build" -DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=87 \
    -DCMAKE_CUDA_COMPILER=/usr/local/cuda/bin/nvcc
  cmake --build "$LLAMA_DIR/build" -j"$(nproc)" --target llama-server
else
  echo ">> reusing $LLAMA_DIR/build/bin/llama-server"
fi

if [ ! -f "$MODEL_DIR/$MODEL_FILE" ]; then
  echo ">> downloading $MODEL_FILE"
  curl -L -C - -o "$MODEL_DIR/$MODEL_FILE" "$MODEL_URL"
else
  echo ">> reusing $MODEL_DIR/$MODEL_FILE"
fi

if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi
uv tool install --quiet aider-chat || uv tool install aider-chat
AIDER_PY="${HOME}/.local/share/uv/tools/aider-chat/bin/python"
if [ ! -x "$AIDER_PY" ]; then
  AIDER_BIN="$(command -v aider || true)"
  if [ -n "$AIDER_BIN" ]; then
    AIDER_PY="$(sed -n '1s/^#!//p' "$AIDER_BIN")"
  fi
fi
if [ ! -x "$AIDER_PY" ]; then
  echo "aider python not found after uv tool install"
  exit 1
fi
if [ ! -d "$WORKSPACE/.git" ]; then
  git -C "$WORKSPACE" init
  git -C "$WORKSPACE" -c user.email=hearth@local -c user.name=hearth commit --allow-empty -m "hearth workspace"
fi

cat > "$HOME/.config/hearth/aider.yml" << EOF
openai-api-base: http://127.0.0.1:${LLAMA_PORT}/v1
openai-api-key: local
model: openai/qwen
weak-model: openai/qwen
editor-model: openai/qwen
edit-format: whole
map-tokens: 1024
auto-commits: false
no-show-model-warnings: true
EOF

cat > "$BIN/hearth-aider" << EOF
#!/usr/bin/env bash
export OPENAI_API_BASE="\${OPENAI_API_BASE:-http://127.0.0.1:${LLAMA_PORT}/v1}"
export OPENAI_API_KEY="\${OPENAI_API_KEY:-local}"
exec aider --config "\$HOME/.config/hearth/aider.yml" "\$@"
EOF
chmod +x "$BIN/hearth-aider"

cat > "$BIN/hearth-chat" << EOF
#!/usr/bin/env bash
URL="\${HEARTH_URL:-http://127.0.0.1:${PORT}}"
if command -v firefox >/dev/null; then exec firefox --new-window "\$URL"; fi
exec xdg-open "\$URL"
EOF
chmod +x "$BIN/hearth-chat"

# Replace the old LFM/qwen user units so :8082 and :8090 belong to hearth.
systemctl --user stop lfm25-portal.service qwen35-server.service 2>/dev/null || true
systemctl --user disable lfm25-portal.service qwen35-server.service 2>/dev/null || true

sed -e "s|@HOME@|$HOME|g" \
    -e "s|@LLAMA_DIR@|$LLAMA_DIR|g" \
    -e "s|@MODEL@|$MODEL_DIR/$MODEL_FILE|g" \
    -e "s|@LLAMA_PORT@|$LLAMA_PORT|g" \
    "$SRC/pack/qwen.service" > "$HOME/.config/systemd/user/hearth-qwen.service"

sed -e "s|@HOME@|$HOME|g" \
    -e "s|@PREFIX@|$PREFIX|g" \
    -e "s|@WORKSPACE@|$WORKSPACE|g" \
    -e "s|@PORT@|$PORT|g" \
    -e "s|@LLAMA_PORT@|$LLAMA_PORT|g" \
    -e "s|@AIDER_PY@|$AIDER_PY|g" \
    "$SRC/pack/hearth.service" > "$HOME/.config/systemd/user/hearth.service"

loginctl enable-linger "$USER" >/dev/null 2>&1 || true
systemctl --user daemon-reload
systemctl --user enable --now hearth-qwen.service hearth.service

# Wait for llama then hearth
ok=0
for i in $(seq 1 90); do
  if curl -sf --max-time 2 "http://127.0.0.1:${LLAMA_PORT}/v1/models" >/dev/null 2>&1; then
    ok=1
    break
  fi
  sleep 2
done
[ "$ok" = 1 ] || { echo "llama-server did not come up"; systemctl --user status hearth-qwen.service --no-pager | tail -20; exit 1; }
systemctl --user restart hearth.service
sleep 1

HOST="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo
echo "hearth is up"
echo "  chat     http://127.0.0.1:${PORT}"
echo "  tailnet  http://$(hostname):${PORT}  (or MagicDNS :${PORT})"
echo "  chat is Aider (same as: cd $WORKSPACE && hearth-aider)"
echo "  workspace $WORKSPACE"

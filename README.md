# hearth

Local coding agent for an NVIDIA Jetson (Orin Nano and friends). One command
installs **Qwen** and a **Quinovo-look chat that is Aider**.

The browser UI is the Aider harness: same model, same workspace, same
whole-file edits, with every add/apply/diff shown as a tool card. No cloud
inference. Reach it over Tailscale.

## One command

On the Jetson:

```bash
git clone https://github.com/cfollette18/hearth.git
cd hearth
./install.sh
```

That reuses `~/llama.cpp` and `~/models/Qwen3.5-4B-Uncensored-Q4_K_M.gguf` if
they already exist. Otherwise it builds llama.cpp with CUDA and downloads the
GGUF.

Then open:

```
http://127.0.0.1:8090
```

Over Tailscale: `http://<jetson-magicdns>:8090`.

## What you get

| Piece | What it is |
|---|---|
| `hearth-qwen.service` | llama.cpp server, Qwen3.5-4B Q4_K_M, `:8082` |
| `hearth.service` | Quinovo-look chat on `:8090`, driven by Aider |
| Chat UI | Oat/navy/lava, tool rows, lava caret, unified diffs |
| Harness | Aider 0.86 (`edit-format: whole`) against `openai/qwen` |
| `hearth-aider` | Same Aider in the terminal |

Workspace defaults to `~/hearth-workspace` (a git repo). Aider cannot write
outside it.

## Terminal

```bash
cd ~/hearth-workspace
hearth-aider
```

## Environment

| Variable | Default |
|---|---|
| `HEARTH_WORKSPACE` | `~/hearth-workspace` |
| `HEARTH_PORT` | `8090` |
| `HEARTH_LLAMA_PORT` | `8082` |
| `HEARTH_MODEL_FILE` | `Qwen3.5-4B-Uncensored-Q4_K_M.gguf` |
| `HEARTH_MODEL_URL` | Hugging Face GGUF for that file |

## License

MIT

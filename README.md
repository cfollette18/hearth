# hearth

Local coding agent for an NVIDIA Jetson (Orin Nano and friends). One command
installs **Qwen**, a **Quinovo-look chat** that shows every tool call and diff,
and **Aider** pointed at the same model.

No cloud inference. Chat and tools stay on the box. Reach it over Tailscale.

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
| `hearth.service` | Chat + agent on `:8090` |
| Chat UI | Same layout as Quinovo chat: oat/navy/lava, tool rows, lava caret |
| Tools | `read_file`, `write_file`, `edit_file`, `list_dir`, `grep`, `bash` |
| Diffs | Writes and edits show unified diffs in the thread |
| `hearth-aider` | Aider CLI against the same Qwen endpoint |

Workspace defaults to `~/hearth-workspace`. The agent cannot write outside it.

## Aider (terminal)

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

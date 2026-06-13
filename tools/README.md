# Tools

MCP (Model Context Protocol) tool servers for OpenNewton agents. Each server
advertises a native function-calling schema (`tool_configs`) via `initialize`,
so the planner selects a tool and fills arguments — it never writes JSON by
hand. The planner-facing usage guidance for each tool lives in
[`../planner_skills/`](../planner_skills/).

## Directory Structure

```
tools/
├── image/                  # image generation / editing
│   ├── core.py             # ImageCore: OpenAI-compatible generate/edit client (self-contained)
│   └── server.py           # FastMCP server: img_create, make_keyframes
├── image_search/           # web image search (real reference photos)
│   ├── core.py             # ImageSearchCore: Serper /images + local download cache (self-contained)
│   └── server.py           # FastMCP server: image_search
├── simulator/              # Genesis physics simulation
│   ├── core.py             # SimCore: scene_spec -> Genesis scene, run, trajectories (self-contained)
│   └── server.py           # FastMCP server: simulate
└── video_verifier/         # loop infra (NOT a planner tool — see note below)
    └── core.py             # VideoPhy2Client (SA/PC) + GeminiVerifierCore (blind A/B + condition judge)
```

> **`video_verifier` is loop infrastructure, not a planner tool.** Unlike the
> tools above, the planner never function-calls it: the inference loop runs the
> scorers automatically and feeds the result back into memory. So it has **no MCP
> server, no `tool_configs`, no planner skill**.

## Tool Servers

| Server | Tools | Planner skill |
|--------|-------|---------------|
| `image/server.py` | `img_create`, `make_keyframes` | `planner_skills/Img_create.md`, `planner_skills/make_keyframes.md` |
| `image_search/server.py` | `image_search` | `planner_skills/Image_search.md` |
| `simulator/server.py` | `simulate` | `planner_skills/simulator.md` |

- **`img_create`** — create ONE image; text-to-image with no reference, or an
  edit when `reference_images` is given. Returns the local path.
- **`make_keyframes`** — create a FIRST + LAST key-frame pair; the last frame is
  generated as an edit of the first so they stay consistent. Returns both paths
  in order for the video generator.
- **`image_search`** — text-to-image web search for a REAL reference photo.
  Downloads each result to a local path so it can condition `img_create` or the
  video generator. Returns `{title, url, page_url, local_path}` records.
- **`simulate`** — run a Genesis physics simulation of a structured `scene_spec`
  (objects + materials + initial state). Returns per-object trajectories and a
  summary; `render: true` also attempts an isolated 24fps GPU reference render
  by default, so a 5s scene produces 120 frames. If the headless render stack
  stalls, the worker times out and the trajectory still returns. Handles rigid
  bodies and fluids/granular/soft bodies (SPH/MPM/PBD), routed automatically by
  each object's `material`.

## Configuration

Image tools read an OpenAI-compatible image API from env (or `initialize` args):
`IMG_CREATE_API_KEY`, `IMG_CREATE_BASE_URL`, `IMG_CREATE_MODEL`, and optionally
`IMG_CREATE_AUTH_HEADER`, `IMG_CREATE_SUPPORTED_SIZES`, `IMG_CREATE_RESOLUTION`,
`IMG_CREATE_OUTPUT_DIR`. See [`../.env.template`](../.env.template).

Image search reads a Serper.dev (or compatible) backend from env (or
`initialize` args): `SERPER_API_KEY` (required), and optionally `SERPER_BASE_URL`
(default `https://google.serper.dev`) and `IMAGE_SEARCH_OUTPUT_DIR` (default
`./outputs/image_search`).

The simulator needs the **Genesis** package (run it in the `cu128torh2_10` conda
env, which has `genesis` + `mcp` installed) and a CUDA GPU. Optional env:
`SIMULATOR_BACKEND` (`gpu`|`cpu`, default `gpu`), `SIMULATOR_OUTPUT_DIR`,
`SIMULATOR_RENDER_FPS` (default `24`), `SIMULATOR_RENDER_TIMEOUT` (default
`180`), and `SIMULATOR_RENDER_ISOLATED` (`1` by default; set `0` only for
direct render debugging).

The verifier has two scorers. `VideoPhy2Client` reads the VideoPhy2 service URL(s)
from `VIDEOPHY2_URL` (comma-separated for several instances) and returns absolute
`{sa_score, pc_score}` (1-5). `GeminiVerifierCore` reads a Gemini-compatible
`generateContent` backend: `GEMINI_API_KEY` (required), optional `GEMINI_BASE_URL`
and `GEMINI_MODEL`; it provides `verify_relative` (blind A/B candidate-vs-baseline,
score in [-10,+10]) and `judge_condition` (pre-generation soundness check). Inline
video is base64'd into the request, so keep clips under ~18 MB.

## Run

```bash
# MCP stdio server
python tools/image/server.py

# Local smoke check (needs IMG_CREATE_* env set)
python tools/image/server.py --test

# Image search (needs SERPER_API_KEY set)
python tools/image_search/server.py            # MCP stdio
python tools/image_search/server.py --test     # smoke check

# Simulator (Genesis): run in the cu128torh2_10 conda env
python tools/simulator/server.py            # MCP stdio
python tools/simulator/server.py --test     # smoke check

# Video verifier (needs GEMINI_API_KEY set) — CLI smoke check, no server
python tools/video_verifier/core.py <video.mp4> "<the original question>"
```

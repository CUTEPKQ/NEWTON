---
description: Set up the Newton environment — create conda env, install dependencies, and configure API keys. Use when the user wants to set up, install, or configure Newton.
---

You are helping the user set up the Newton physics-video generation system. Walk through each step below. If a step is already done, skip it and say so.

## Step 1 — Conda environment

Check if a conda env named `newton` exists (`conda env list`). If not, create it:

```
conda create -n newton python=3.10 -y
```

Then activate it. Confirm Python 3.10 is active.

## Step 2 — PyTorch

Check if PyTorch is installed (`python -c "import torch; print(torch.__version__, torch.cuda.is_available())"`).

If not installed, detect the CUDA version (`nvidia-smi` or `nvcc --version`) and install the matching PyTorch build. Example for CUDA 12.8:

```
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu128
```

If no GPU is available, install the CPU build.

## Step 3 — Python dependencies

```
pip install -r requirements.txt
```

## Step 4 — API keys

Check if `.env` exists in the project root. If not, copy from `.env.template`:

```
cp .env.template .env
```

Then ask the user for each API key group, one at a time. Explain what each service is used for, and write the values into `.env`. The groups are:

1. **Planner LLM** (PLANNER_API_KEY, PLANNER_BASE_URL, PLANNER_MODEL) — the agent brain, any OpenAI-compatible endpoint. Ask the user which provider they use (OpenAI, Azure, or a third-party gateway) and set PLANNER_AUTH accordingly (bearer or api-key).
2. **Gemini** (GEMINI_API_KEY) — used for the video verifier (blind A/B scoring) and optionally for image generation. Get a key at https://ai.google.dev/gemini-api/docs.
3. **Seedance** (SEEDANCE_API_KEY, SEEDANCE_HOST) — the video generation backend. Get access at https://seed.bytedance.com/en/seedance2_0.
4. **Serper** (SERPER_API_KEY) — web image search. Get a key at https://serper.dev/.
5. **Img_create** (IMG_CREATE_API_KEY, IMG_CREATE_BASE_URL, IMG_CREATE_MODEL, IMG_CREATE_API) — image generation/editing. Can share credentials with the Planner or Gemini endpoint. Ask the user which provider they want to use.

For any key the user does not have yet, leave the placeholder and tell them they can fill it in later — only PLANNER and SEEDANCE are strictly required to run the basic loop.

## Step 5 — Verify

Run a quick sanity check:

```
python -c "from loop.run_loop import build_tool_configs, load_skill_catalog; print('tool configs:', len(build_tool_configs())); print('skills:', list(load_skill_catalog().keys())); print('OK')"
```

If this prints OK, tell the user setup is complete and they can use `/run` to generate their first physics video.

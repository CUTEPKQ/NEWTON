---
description: Run the Newton inference loop to generate a physics-grounded video. Use when the user wants to generate a video, run an experiment, or test a scenario.
---

You are helping the user run the Newton physics-video generation loop. Follow each step below.

## Step 1 — Pre-flight checks

Verify the environment is ready:

1. Check that `.env` exists in the project root. If not, tell the user to run `/setup` first.
2. Scan `.env` for placeholder values (lines still containing `<your-`). Warn if any **required** keys are missing:
   - `PLANNER_API_KEY` and `PLANNER_BASE_URL` — required
   - `SEEDANCE_API_KEY` and `SEEDANCE_HOST` — required
   - Others are optional but enable more tools (tell the user which tools will be unavailable).
3. Check that the `newton` conda environment is active. If not, activate it.

## Step 2 — Scenario input

Ask the user: **"What physics scenario do you want to generate?"**

Encourage them to describe a concrete physical interaction — objects, materials, and what happens. Give a few examples to inspire them:

- "A glass marble rolls down a wooden ramp and launches off the end"
- "A water balloon drops onto a table and bursts"
- "A pendulum swings and knocks over a row of dominoes"

Once they provide a description, help refine it if needed — the prompt should be specific about:
- Object types and materials
- Initial positions and motions
- The key physical interaction (collision, deformation, fluid flow, etc.)
- Expected outcome

Confirm the final scenario text with the user before proceeding.

## Step 3 — Configure run options

Ask the user if they want to adjust any settings, or use defaults:

| Flag | Default | Description |
|------|---------|-------------|
| `--max-turns` | 8 | Maximum planner/executer/verifier rounds |
| `--duration` | 5 | Generated video length in seconds |
| `--out-dir` | `outputs/loop` | Output directory |
| `--baseline` | (none) | Path to an existing baseline mp4 to skip baseline generation |

Most users should use the defaults. Only ask about these options once — don't repeat.

## Step 4 — Run the loop

Execute the inference loop:

```
python loop/run_loop.py "<scenario>" --max-turns <N> --duration <D> --out-dir <dir>
```

The loop will take several minutes. While it runs:

- Watch for each turn's output and report progress to the user (turn number, which tools the planner called, verification scores).
- If the run errors out, diagnose the issue. Common problems:
  - API key invalid or expired → tell the user which key to check
  - Rate limit hit → suggest waiting and retrying
  - CUDA out of memory → suggest reducing resolution or closing other GPU processes

## Step 5 — Report results

When the loop finishes:

1. List the output directory contents so the user can see what was generated.
2. Tell the user where to find:
   - The final generated video
   - The baseline video (for comparison)
   - `trace.json` (full trace of tool calls and scores per turn)
3. Report the final verification score and how many turns it took.
4. If the verifier score was below the STOP threshold (5), mention that the user can re-run with more turns or a refined scenario.

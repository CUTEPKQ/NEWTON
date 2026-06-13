---
name: prompt_refine
description: Write or rewrite the single video_prompt — the natural-language caption fed to the video generator as-is. A no-tool skill whose output IS the refined text. Use to produce the initial caption for a scenario, or to fix a caption the verifier judged wrong or physically implausible.
---

# prompt_refine

`prompt_refine` is a **no-tool** skill: the planner directly writes or rewrites the single `video_prompt` — the natural-language caption fed to the video generator (seedance 2.0) as-is. There is no function to call and nothing executes; the output of this skill **is** the refined prompt text itself.

Use it to turn the scenario (and any verifier feedback, computed physics, or existing key frames) into one clean cinematic caption that drives the video, or to correct a prompt the verifier judged physically implausible.

## When to use

- You need to produce the initial `video_prompt` for the scenario, or
- You need to revise an existing `video_prompt` because the verifier reported a prompt-level problem (wrong object, missing element, wrong action, weak motion, physical implausibility).

## When NOT to use

- The key frames themselves are wrong and need regenerating → use `make_keyframes` / `img_create`.
- The motion / physics is complex (collisions, fluids, soft-body, exact counts) and needs a physics-correct reference → use `simulate`.
- You need a real reference photo → use an image-search skill.

Those fix images/motion; this fixes the *text*.

## Instructions
Write ONE natural caption that describes the scene **chronologically** — what is there, what happens, and how the physical state changes from start to finish.

Before writing, first identify which condition material is staged **this round**. Use exactly the matching reference grammar:

| Staged material this round | What to write in `video_prompt` |
|---|---|
| No references | Describe the full scene and action directly. Do not mention `Video 1` or `[Image N]`. |
| `ref_video` only | Open with `Reference Video 1's ...` and state the motion/timing/count/layout to copy. Do not mention `[Image N]`. |
| `first_frame` and optionally `last_frame`, with no `ref_video` | Do not mention `Video 1` or `[Image N]`. The frame(s) are implicit i2v endpoints; if `last_frame` exists, describe only the transition from first to last; if only `first_frame` exists, describe the motion starting from that frame toward the scenario's final state. |
| `ref_images` only | Cite each used image as `[Image 1]`, `[Image 2]`, etc., and state what appearance/count/layout each supplies. Do not mention `Video 1`. |
| `ref_video` plus images/keyframes | Cite `Reference Video 1` for motion/timing/count/layout, and cite `[Image N]` for appearance. Make sure they describe the same object count and scene. |

1. **Coverage.** The caption MUST include the original scenario's main objects, the action(s), and the physical state changes (before → during → after). Nothing essential from the scenario may be dropped.
2. **Length.** One fluent description, not a list. No hard word limit; be as long as the scene needs and no longer.
3. **Motion.** Describe real motion and its consequences. Do NOT describe a generic static / no-motion scene unless the original scenario itself is static.
4. **Physical plausibility.** Make the dynamics consistent — gravity, contact, momentum, deformation, spill/splash, settling. If a `simulate` reference produced concrete motion (trajectories, timing, where things land), embed that concretely into the caption.
5. **No planner labels.** Never write schema/label text inside the caption: no `start:`, `key_event:`, `end:`, "target physical state", "keyframe", "generic filler", or any instruction-to-self.
6. **No repetition.** Do not repeat phrases or pad with content-free wording ("a purely visual experience", "a scene unfolds"). Every clause should add concrete visible information.

### Reference video grammar
Use this only when `ref_video` is staged this round. Seedance follows a reference video's motion only if the prompt explicitly names it. A reference clip that is attached but unnamed is often ignored.

> *"Reference Video 1's `<what to follow: the motion / timing / object count / layout>`, generate `<the real-world scene>`, keeping `<the referenced aspect>` consistent."*

- Name it as **"Video 1"** (the staged clip is always Video 1).
- State **exactly what to copy** from it — for a physics clip that is the **motion/timing**, and if the scene has a countable number of objects, also **the exact count and layout** ("the exact eight tiles in one row, same number and spacing").
- THEN describe the full real-world scene (environment, materials, lighting) as usual — the reference supplies motion, your words supply appearance.
- Do not contradict the reference (don't ask for a different count or a different action than the clip shows).
- If there is no `ref_video`, never mention `Video 1`.

Example (reference sim clip of 8 dominoes toppling):
> "Reference Video 1's domino chain-reaction motion, timing, and its exact layout of eight tiles in one straight row — match the number of tiles and the order they fall. Generate it in a warm sunlit room: eight ivory dominoes on a polished wooden table with visible grain, a softly blurred home interior behind. Each tile pivots on its bottom edge and taps the next until the last falls flat. Photorealistic, natural daylight."

### Image and keyframe grammar
How a staged image must be referenced depends on how the orchestrator will send it to Seedance. Use the decision table above first; never write the grammar of one scenario for another.

**First/last frame mode — `first_frame` / `last_frame`, and NO simulation video.**
The frame(s) become implicit i2v endpoint(s): the video starts on `first_frame` and, if present, ends on `last_frame`.
- Do **NOT** write `[Image 1]` / `[Image 2]` and do **NOT** mention "Video 1" — the frames are
  implicit endpoints, not cited references.
- Describe ONLY the **motion in between** — how the scene evolves from `first_frame` to
  `last_frame` when both are staged (trajectory, deformation, timing). If only `first_frame`
  is staged, describe the continuation from that starting state to the scenario's final state.
- Use this when the phenomenon can't be simulated (optical effects, phase changes, reflections)
  and you anchored the start/end with keyframes instead.

> Example (keyframes of a soap bubble before/after it pops, no sim):
> "The soap bubble trembles, its rainbow surface shimmering, then bursts — the film tears open and the thin shell collapses into a fine spray of tiny droplets that scatter outward and vanish. Soft daylight, photorealistic, shallow depth of field."

**Reference-image mode — `ref_images`, or keyframes demoted because a `ref_video` is also staged.**
Here the images are appearance/layout references, not endpoints. Cite each used image with Seedance's `[Image 1]`, `[Image 2]` grammar, state what each supplies, and keep the caption consistent with it.

> *"[Image 1]'s clay duck — same shape, color and texture — dropping onto a wooden table and denting, warm studio light."*

#### Reference video plus reference images — cite BOTH
The common strong case: a `simulate` motion clip PLUS an appearance image. Reference **both**
channels in one caption and make them agree:
- cite **"Video 1"** for the MOTION / timing / count, and
- cite **"[Image N]"** for the APPEARANCE of the subject, and
- ensure they don't contradict (the thing moving in Video 1 is the same thing shown in
  [Image 1] — same count, same object). Text, video and image must all describe the SAME scene.

> Example (sim clip of a clay duck denting + an appearance image of the duck):
> "Reference Video 1's drop-and-squash motion and timing — the duck falls, hits the table and dents permanently without bouncing. The duck looks exactly like [Image 1]: a small yellow modelling-clay figurine with soft matte texture. A warm sunlit workshop, wooden plank table with visible grain. Photorealistic, natural daylight."

### Revising on verifier feedback
- Read the verifier's specific complaint and fix exactly that in the text:
  * **wrong / missing object** → name and place the correct object explicitly.
  * **wrong action** → restate the action and its chronological progression.
  * **physically implausible** → correct the dynamics (direction of fall, where things land, how liquids/cloth/debris respond).
  * **wrong reference grammar** → remove references to unavailable material immediately (for example, delete `Reference Video 1` when no `ref_video` is staged).
- Keep everything the verifier did NOT complain about; change only what is needed.

## Output
The skill produces `video_prompt`: the one refined caption (string). No tool call is emitted —
the text IS the deliverable, passed straight to the video generator. (Whether staged images
act as the i2v first frame or as reference images is decided by how the material was produced,
not by this caption — just describe the scene and cite the references.)

## Examples
Scenario: "A glass of water tips over on a wooden table and spills."
> "A clear glass half-full of water stands upright on a wooden table in soft daylight. It begins to tip sideways, water sloshing toward the rim, then topples onto its side; the water pours out and spreads into a widening puddle across the grain, a few droplets scattering, the empty glass rocking to a stop beside the spill."

Revision after verifier: "video is static, the glass never falls."
> "A clear glass half-full of water on a wooden table tilts past its balance point and falls onto its side, the water rushing out and spreading into a glistening puddle, droplets flicking outward as the glass settles."

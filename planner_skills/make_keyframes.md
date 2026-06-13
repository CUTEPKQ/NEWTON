---
name: make_keyframes
description: Create the FIRST + LAST key-frame pair that anchors a video. The last frame is generated as an edit of the first, so both share subject/scene/lighting and differ only in physical state. Use when you have a scenario and need the still frames the video generator conditions on.
---

# make_keyframes

`make_keyframes` produces the **two key frames that anchor a video** — a **first** frame (the pre-action scene) and a **last** frame (the final state after the whole action). The last frame is generated as an **edit of the first**, so both share subject, scene and lighting and differ only in physical state. The video generator (seedance 2.0) interpolates the motion between them.

This is the standard way to create key frames in OpenNewton. Prefer it over calling `img_create` twice yourself — it enforces first→last consistency for you.

**How the frames are used (decides how you must write the `video_prompt`):**
- If you make keyframes and there is **no** simulation video this round, the two frames become
  the video's literal **first and last frame** (strong i2v). In the caption describe ONLY the
  motion in between — do **not** cite `[Image N]` and do **not** mention "Video 1".
- If a **simulation video is also staged**, the frames cannot be first/last (a video can't
  combine with the first/last-frame mode), so they are used as **reference images** instead. In
  that case cite them as `[Image 1]`/`[Image 2]` AND cite the clip as "Video 1" in the caption.
- Use keyframes (no sim) for phenomena the simulator can't produce — optical effects, phase
  changes, reflections — where only the start/end states can be anchored. See `prompt_refine`.

## When to use

- You need the key frames for a new or revised video — i.e. you have a scenario and need the still frames the video conditions on.

## When NOT to use

- The key frames already exist and are correct and only the text needs work → use `prompt_refine`.
- You need just one image or an ad-hoc edit → use `img_create`.
- The motion / physics is complex and needs a physics-correct reference → use `simulate`.
- You need a real reference photo → use an image-search skill.

## Parameters
| Param | Meaning |
|---|---|
| `first_prompt` | Text-to-image caption of the full **pre-action** scene. |
| `last_prompt` | Edit instruction for the **final** visible state, written as an edit of the first frame. |
| `reference_images` | Optional local paths to seed the first frame (e.g. a reference photo). Empty → first frame is text-to-image. |

## Writing `first_prompt` (text-to-image)
- Describe the **initial state only** — the scenario describes the WHOLE motion; do NOT copy it.
- Cover subject, location, key objects, viewpoint, lighting. One fluent sentence/paragraph, ~10–80 words, no phrase repetition.
- No motion blur; this is a still of the moment before the action.

## Writing `last_prompt` (edit of the first frame)
- Describe the **final visible state** after the entire action completes, as a state replacement — the image editor swaps visible states, it cannot perform actions. Prefer:
  * "Change `<subject + related elements>` to `<new visible state>`."
  * "Replace `<old visible state>` with `<new visible state>`."
  * "`<subject + related elements>` are now `<new visible state>`."
  Avoid leading verbs like Fold / Pull / Push / Lift / Strike / Pour / Move.
- **Physical causality** — when the main subject changes, also describe every element that physically responds:
  * the actor's hand / arm / body pose, if interacting with the subject
  * contact effects: dust, ripples, splash, debris, sparks at the impact point
  * secondary responses: cloth folds, hair direction, re-cast shadows, gaps/openings, deformation of contacted surfaces, displaced air or liquid
  * the consequence of removed objects (e.g. an empty hand after a release)
- **Visual distinctness** — the last frame MUST look clearly different from the first (different pose, position, count, or state). If they would look identical, strengthen the change.
- Never embed planner labels or schema text in either prompt ("first:", "last:", "goal:", "target state").

## Output
Returns two records, `index` 0 (first) and 1 (last), each `{prompt, local_path, mode}`. Pass both `local_path`s — first then last — to the video generator.

## Examples
Scenario: "A glass of water tips over on a wooden table and spills."
> first_prompt: "A full glass of water standing upright on a wooden table, side view, soft daylight from the left, calm and still."
> last_prompt: "The glass is now lying on its side on the wooden table, water spilled into a spreading puddle around it, a few scattered droplets, same lighting and viewpoint."

Scenario: "An archer draws a recurve bow and releases the arrow into the target."
> first_prompt: "An archer in a neutral stance on a grass range, holding a relaxed recurve bow, arrow nocked but undrawn, target visible downrange, clear daylight, side view."
> last_prompt: "Change the relaxed bow and empty target to the arrow now embedded in the target center, the bowstring slack and the archer's bow arm still extended in follow-through, faint dust at the target face, same range and lighting."

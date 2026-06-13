---
name: img_create
description: Create or edit exactly ONE still image via an OpenAI-compatible image API. Use when you need a single key frame from text (text-to-image), or an ad-hoc edit of an existing image (image-to-image). Returns the local path for downstream key-frame / video generation.
---

# img_create

`img_create` produces **one image** from a text prompt, optionally conditioned on reference image(s). With no reference it generates from text (text-to-image); with one or more references it edits / re-conditions on them (image-to-image). It returns the local path of the produced image, which downstream steps (the video generator, or a further edit) consume.

This is a single-image tool. It does NOT produce the final video (→ video generator), does NOT search for real photos on the web (→ image_search), and does NOT build the first+last key-frame pair (→ make_keyframes does that with enforced consistency).

## When to use

- You need to create or edit exactly one still image — a single key frame, or an ad-hoc revision of an existing image.

## When NOT to use

- You need the first+last key frames for a video → use `make_keyframes`.
- A correct image already exists and only its surrounding text needs work → use `prompt_refine`.
- You need a real reference photo → use an image-search skill.
- The motion / physics is complex and needs a physics-correct reference → use `simulate`.

## How the tool behaves
- `reference_images` empty → **generate** from `prompt` alone.
- `reference_images` non-empty → **edit**: the prompt describes the new visible state of the given image(s); the model conditions on them for subject/scene/lighting consistency.
- Returns one image and its `local_path`. Call the tool again (passing a prior `local_path` in `reference_images`) to build a consistent sequence.

## Parameters
| Param | Meaning |
|---|---|
| `prompt` | One fluent, concrete description of the single image to produce. For an edit, describe the *resulting* visible state, not the action. |
| `reference_images` | List of local image paths to condition on. Empty → text-to-image. Non-empty → image edit. |
| `as_first_frame` | Optional bool. If true, the image is staged as the video's **first frame** (a STRONG i2v constraint — the video must START on this exact image) instead of a weak reference image. Use when the image already shows the correct scene (e.g. the exact object count/layout) and you want the generator anchored to it. NOTE: a first frame cannot be combined with a simulation reference video (mutually exclusive); if a sim video is also staged, the image is automatically used as a reference image instead. |

## Writing the `prompt`
- One image, one prompt: ~10–80 words, a single descriptive sentence/paragraph. No phrase repetition.
- Be explicit and concrete — replace vague words ("nearby", "some") with specific, visible states (position, pose, count, material, lighting, viewpoint).
- **Generate (text-to-image):** caption the full scene as a still — subject, location, key objects, viewpoint, lighting. No motion blur unless the still itself is mid-motion.
- **Edit (image-to-image):** describe the new visible state as a state replacement, not a real-world action — the model swaps visible states, it cannot "perform" verbs. Prefer:
  * "Change `<subject + related elements>` to `<new visible state>`."
  * "Replace `<old visible state>` with `<new visible state>`."
  * "`<subject + related elements>` are now `<new visible state>`."
  Avoid leading verbs like Fold / Pull / Push / Lift / Strike / Pour / Move.
- When the main subject changes in an edit, also describe every element that physically responds: the actor's hand/arm/pose, contact effects (dust, ripples, splash, debris), secondary responses (cloth folds, re-cast shadows, deformation), and the consequence of removed objects.
- Never embed planner labels or schema text in the prompt ("first:", "last:", "goal:", "target state").

## Output
The tool returns one record: `{prompt, local_path}`. Use `local_path` as input to the next step (the next edit in a chain, or the video generator).

## Examples
Generate a key frame from text:
> prompt: "A full glass of water standing upright on a wooden table, side view, soft daylight from the left, calm and still."
> reference_images: []

Edit an existing image into its later state:
> prompt: "The glass is now lying on its side, water spilled into a spreading puddle around it, a few scattered droplets, same table, lighting and viewpoint."
> reference_images: ["outputs/images/glass_upright.png"]

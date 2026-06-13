---
name: image_search
description: Search the web for a REAL reference image by text query and get its local path. Use when you need an actual photograph/illustration of a specific real-world object, place, person-type, style, or appearance to condition image or video generation — rather than synthesizing one from scratch. Returns local paths to downloaded images.
---

# image_search

`image_search` runs a **text-to-image web search** and returns **real images**
matching your query, each already **downloaded to a local path**. Use it to
ground generation in a real reference — the true look of a specific object,
location, material, brand, or visual style — instead of inventing the appearance.
The returned `local_path`s feed straight into `img_create` (as a
`reference_images` entry) or the video generator.

In the OpenNewton loop, `image_search` stages result **index 0** as the default
reference image. If another thumbnail is better, call `select_reference_image`
with that result's `index` before writing the final `video_prompt`.

This skill finds existing images on the web; it does NOT synthesize new ones
(→ `img_create`), does NOT build the first+last key-frame pair
(→ `make_keyframes`), and does NOT run physics (→ `simulator`).

## When to use

- You need the **real appearance** of something concrete — a particular product,
  landmark, animal, vehicle, costume, art style, texture — to anchor the image
  or video so it looks authentic rather than imagined.
- You want a reference photo to pass into `img_create` for an edit, or to seed a
  consistent key frame.

## When NOT to use

- You want to create or edit an image from a description → use `img_create`.
- You want the first+last key frames for a video → use `make_keyframes`.
- Only the caption text needs work → use `prompt_refine`.
- You need physics-correct motion → use `simulator`.

## How it behaves

- You pass a `query` (and optional `top_k`). The tool queries the web image
  search backend, downloads each result image to a local cache, and returns one
  record per **successfully downloaded** image (failed downloads are dropped).
- Results come back ranked by the search engine's relevance. Image content is
  not under your control — pick the best-matching `local_path` from the returned
  list; refine the `query` if none fit.
- Result index 0 is staged automatically. To use any other result as the video
  reference image, call `select_reference_image({"index": <chosen index>})`.

## Parameters

| Param | Meaning |
|---|---|
| `query` | Descriptive text for the image you need. Be concrete about subject, attributes, and viewpoint (e.g. "red ceramic coffee mug on white background, studio photo"). |
| `top_k` | Optional integer (default 5). Max images to return. |

## Writing the `query`

- **Be specific and visual.** Name the subject plus the attributes that matter:
  color, material, count, setting, and viewpoint ("side view", "top-down",
  "studio white background", "close-up"). A vague query returns mixed,
  unusable hits.
- **Add intent keywords** that bias toward clean references: "studio photo",
  "isolated on white", "product shot", "high resolution", "reference".
- **One subject per query.** If you need several different references, call the
  tool once per subject rather than cramming them into one query.

## Budget — searches are limited, use them sparingly

The search backend has a quota; each `image_search` call costs one search credit
regardless of `top_k`. Be economical:

- **1–2 calls is normally enough** for a whole task. Don't re-search the same
  subject hoping for a better hit — refine the `query` once, not repeatedly.
- **Pick exactly one image per call** in the common case: choose the single
  best-matching `local_path` and move on. Only keep more than one if the extras
  cover genuinely different essentials (e.g. front vs. side view you both need).
- **Don't search for things you can describe.** Only reach for `image_search`
  when the real appearance actually matters (a specific named character, person,
  brand, landmark); for a generic object, describe it to `img_create` instead.

## Output

Returns `records`, a list of `{index, title, url, page_url, local_path, thumbnail}`,
plus an `images` list of the thumbnails in index order:

- `index`: stable position used to refer to a result ("I pick image [1]").
- `thumbnail`: a **small inline preview** of the image — you actually SEE each
  candidate, so choose by looking, not by guessing from the title.
- `local_path`: the **downloaded full image on disk** — this is what you pass
  downstream (to `img_create` `reference_images`, or the video generator's
  reference image).
- `title` / `page_url`: caption and source page (attribution / relevance).
- `url`: the original remote image URL (informational; prefer `local_path`).

**Look at the thumbnails and pick the single best match.** If index 0 is best,
do nothing else; it is already staged as the reference image. If another result
is better, call `select_reference_image` with that index. If none fit, refine the
`query` and search again (sparingly — see the budget note above).

## Downstream use — where the `local_path` goes

A searched image is a real-world reference; there are three ways to use it:

1. **Straight to the video generator as a `reference_image`** (most direct). Pass
   the chosen result as the video generator's **reference image** — it borrows the
   subject's *appearance/identity/style* only, and you describe the scene and
   action entirely in the text prompt. Use this when you just need the generated
   video's subject to *look like* the searched image (a specific character,
   person, product, creature) but the scene is yours to define. If the chosen
   result is not index 0, call `select_reference_image` first.
2. **Edit it first with `img_create`, then generate.** When you need the subject
   in a *specific state/pose/scene* before generating (e.g. the mug tipped over),
   feed the `local_path` into `img_create` as a `reference_images` entry, get the
   edited still, then send that to the video generator.
3. **As an `img_create` reference for a still only** (no video).

**Reference image, NOT first frame.** Pass a searched image as the video
generator's **reference_image**, never as its **first_frame**. A web image's
initial scene (white background, a random pose, a product shot) is usually not
your target opening scene; using it as the first frame locks the video to start
from that exact picture. As a reference_image it only contributes appearance, and
the text prompt is free to set the actual scene and motion. Only use a real image
as a first_frame if that image genuinely *is* the desired opening frame.

## Examples

Fetch a real product reference to condition an edit:

> query: "red ceramic coffee mug, isolated on white background, studio product photo"
> top_k: 5

Then pass the chosen `local_path` into `img_create`:

> prompt: "The same red ceramic mug, now tipped over with coffee spilling into a spreading puddle on a wooden table, side view, soft daylight."
> reference_images: ["outputs/image_search/<hash>.jpg"]

Fetch a real character reference and send it straight to the video generator as
a reference image (appearance only — the scene and action come from the text):

> query: "Pikachu, official Pokemon art, full body, white background"
> top_k: 3

Then, to the video generator: text = "the yellow mouse character runs across a
sunny meadow and jumps over a log", reference_image = "outputs/image_search/<hash>.png"
(do NOT pass it as first_frame — the white-background art is not the opening scene).

If the best thumbnail was result `[2]`, first call:
> select_reference_image index: 2

Fetch a real location/style reference for a scene:

> query: "modern bowling alley lane, polished wood, side view, photorealistic"
> top_k: 3

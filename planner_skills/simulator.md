---
name: simulator
description: Run a Genesis physics simulation of a scenario and return a physics-correct reference video (plus per-object trajectories). Use for almost any RIGID-BODY mechanics scenario — free fall and projectile arcs, bouncing, collisions and momentum transfer (one ball striking another, Newton's-cradle), friction/sliding/rolling down ramps, stacking/toppling (dominoes), pendulums and any Newton's-three-laws setup — and for ALL fluid/granular/soft-body/cloth behavior (pouring liquid, sand collapse, clay denting, cloth drape). It nails timing, trajectories, collision response and deformation that text-to-video gets wrong. Only skip it for a single lone object in trivial free motion with NO collision, or for purely human/vehicle motion.
---

# simulator

`simulator` runs a **physics simulation** of the scenario in the Genesis engine and returns a
**physics-correct reference video** of the motion (plus per-object trajectories). You describe
the scene as a structured `scene_spec` (objects, materials, initial state, gravity);
the engine integrates the real dynamics (gravity, contact, collision, fluid/granular flow,
deformation) and renders what actually happens. That rendered clip is the main deliverable —
feed it to the video generator as a motion reference so the generated video follows true
physics instead of a hand-drawn trajectory.

## What the sim models — and what it does NOT

The sim models ONLY the **core physical motion** of the objects. It does NOT need
to depict the external agent or apparatus that sets the motion off (a hand, a
finger, a launcher, a cue, a gust). Do NOT add proxy bodies to stand in for such
an agent. Instead, encode whatever the agent does as the affected object's
**initial condition** — its `init_velocity` / `init_angular` at t=0 (e.g. the
first domino simply starts with a small tipping `init_angular`; a struck ball
starts with an `init_velocity`). The sim then plays out the real dynamics that
follow. The *appearance* of the trigger (the actual hand, the splash of contact,
who pushed it) is supplied later by the prompt / keyframe images, NOT by the sim.
A clean sim of just the objects' motion is correct and preferred; a sim cluttered
with stand-in "pusher" objects is wrong.

## When to use

- **Rigid-body mechanics** — basically any Newtonian-physics scenario: free fall & projectile
  arcs, bouncing, **collisions and momentum transfer** (one ball striking another, a
  Newton's cradle), **friction / sliding / rolling** (down a ramp, across a surface into a
  wall), **stacking / toppling** (a row of dominoes, a tower collapsing), pendulums, levers,
  inclined planes — any demonstration of Newton's three laws. These have crisp, correct
  answers that physics gives you and text-to-video routinely gets wrong (bad timing, no
  recoil, balls passing through each other), so simulate them.
- **Any fluid / granular / soft-body / cloth** behavior — liquid pour/splash, sand collapse,
  clay/dough denting, jelly wobble, cloth drape.
- You want a physics-correct reference clip to condition the video generator, or a
  time-resolved trajectory to ground the caption.

## When NOT to use

- The motion is ONLY simple rigid motion of a SINGLE object with NO collision — one lone object
  falling / sliding / rolling through free space, ordinary human or vehicle motion → just write
  the caption. (The moment a second body is struck — a ball hitting another ball, a collision,
  a momentum transfer, toppling a row — it is multi-body contact: simulate it, even if each body
  is "just rolling". "Rolls and strikes" is NOT simple rolling.)
- You only need still key frames → use `make_keyframes` / `img_create`.
- You only need to fix the caption text → use `prompt_refine`.
- You need a real photo → use an image-search skill.

Reach for `simulator` whenever the motion is hard for the video generator to get right on its
own: contact-rich / multi-body dynamics, exact object counts, or ANY fluid / granular (sand,
snow, powder, dust) / soft-body (clay, dough, jelly) / cloth behavior — these almost always
come out physically wrong from text alone, so simulate them; do NOT settle for a text-only
description of such motion.

## How it behaves

- You pass a `scene_spec` (schema below). The engine builds the scene from primitive shapes,
  applies each object's material and initial state, steps the simulation for the fixed clip
  length (3 s), and samples each non-fixed object's state every step.
- It **always** returns the `local_path` of a rendered video (physics-correct motion) — the
  reference clip for the video generator — plus the per-object **trajectory** and a short physics
  `summary`. Rendering is automatic for every simulation; you do not request it.
- The simulation is deterministic given the same `scene_spec`.
- Every material renders visibly with no extra setup: rigid bodies and soft/elastic and cloth
  bodies as smooth lit surfaces; fluids and granular (liquid/sand/snow) as colored particle
  clouds. The camera auto-frames the whole motion so nothing leaves the view —
  by default a **side-and-above** view (looking from the side, slightly raised)
  that reads vertical motion and how material lands/spreads most clearly. You
  normally don't touch the camera; advanced overrides exist if needed
  (`camera_view: "side"|"three_quarter"`, `camera_height_frac`, `camera_fov`,
  `camera_margin`).

## Parameters

| Param | Meaning |
|---|---|
| `scene_spec` | The structured scene description (objects + global settings). Schema below. |
| `render_fps` | Optional number (default 24). Reference-video frame rate. |

### `scene_spec` schema

```json
{
  "objects": [
    {
      "id": "ball",            // your label, used as the trajectory key
      "type": "sphere",        // sphere | box | cylinder | mesh
      "material": "rigid",     // rigid | liquid | sand | snow | elastic | cloth
      "pos": [0.0, 0.0, 1.0],  // initial center position in METERS [x,y,z], z up
      "radius": 0.05,          // sphere/cylinder; meters
      "size": [0.1,0.1,0.1],   // box; meters (for a particle body this is the initial volume)
      "asset": "duck",         // type=mesh only: a built-in object (duck|bunny|dragon|sphere)
      "scale": 0.1,            // RIGID mesh only; IGNORED for soft/granular (MPM) meshes (auto-sized)
      "euler": [0,0,0],        // optional orientation [rx,ry,rz] in DEGREES (e.g. tilt a ramp)
      "density": 1000.0,       // optional, kg/m^3 (water≈1000, wood≈600, steel≈7800)
      "init_velocity": [0,0,0],// optional [vx,vy,vz] m/s at t=0 (ALL bodies — rigid AND particle)
      "init_angular": [0,0,0], // optional [wx,wy,wz] rad/s at t=0 (RIGID bodies only; ignored for particles)
      "color": [0.2,0.5,0.95], // optional [r,g,b] 0–1 render color (any material, incl. rigid)
      "transparent": false,    // RIGID only: see-through (glass-like container/tank)
      "opacity": 0.3,          // RIGID only: 0–1 alpha when transparent (lower = clearer)
      "release_after_settle": false, // hold this body frozen during settle, then drop it (needs settle_first)
      "fixed": false           // true → immovable (table, container)
    }
  ],
  "gravity": [0.0, 0.0, -9.81],// optional, default earth gravity
  "settle_first": false       // run physics until liquids are at rest BEFORE recording (calm pool)
}
// Note: a ground floor at z=0 is ALWAYS present automatically — never add it.
// Note: clip duration (3.0s) and fps (24) are FIXED by the tool — not settable.
```

## Writing the `scene_spec`

- **Decompose the scenario into primitives.** Reduce every object to the closest primitive
  shape with real-world dimensions in **meters**: a ball → `sphere` (radius); a tabletop or
  block → `box` (size); a glass/cup → a `box` or `cylinder` container marked `fixed: true`.
  The ground floor is always present automatically (at z=0) — never add it yourself.
  The simulator needs physics, not appearance — abstract shapes are correct here.
- **If the action happens on a SINGLE contact surface, do NOT create that surface — use
  the ground floor.** When everything just rests on / drops onto / topples on ONE flat
  surface (a "table", "floor", "ground"), don't add a `box` for it: place the objects
  directly on the implicit ground floor at z=0 (`pos_z = half_height`) and describe the
  surface's real look (wooden table, etc.) only in the `video_prompt`. Adding a thin `box`
  table here causes worse physics — fast objects and especially particle bodies
  (sand/liquid/snow) can punch THROUGH a thin slab, whereas the ground floor is a solid
  analytic plane nothing can pass. Only add a real `fixed` body when the scene genuinely
  needs a SECOND surface or a container (a cup the water pours into, a ramp it slides down,
  a tank wall) — i.e. more than one contact surface.
- **For a few common real objects you can use a built-in mesh** instead of a primitive:
  `type:"mesh"` with `asset` one of `duck | bunny | dragon | sphere`. No file import needed. Use
  these only when the recognizable shape matters; otherwise a primitive is cheaper and clearer.
  **Sizing:** for a SOFT/granular (MPM) mesh (clay/jelly/etc.) do NOT set `scale` — the body is
  auto-sized to a fixed, clearly-visible size. For a RIGID mesh, `scale` is a uniform factor you
  set yourself.
- **Pick the right material:**
  | `material` | Use for |
  |---|---|
  | `rigid` | solid objects that keep their shape (ball, block, table, cup) |
  | `liquid` | water, juice, any free-flowing fluid |
  | `sand` | granular flow, collapsing piles |
  | `snow` | snow / packing granular |
  | `elastic` | deformable solids (jelly, soft ball) |
  | `cloth` | fabric, sheets, flags |
  For a fluid/granular/soft/cloth body, give its **initial volume** as a `box` `size` at the
  start `pos`; the engine fills that volume with particles and lets them flow/deform.
- **Mix materials freely in one scene — they interact automatically.** You can put rigid,
  liquid, sand, elastic, cloth bodies in the same `objects` list and the engine couples them
  with no extra setup: a rigid ball splashes into liquid, drops into sand, a duck floats in
  water, etc. Just place each object; no flag is needed to enable the interaction.
- **Liquid needs walls to stay deep, or it spreads flat.** A `liquid` body dropped on open
  ground spreads into a shallow puddle. For a **pool/tank** with real depth, surround it with
  `fixed:true` rigid walls (a base + 4 side boxes); add `transparent:true` to the walls to see
  the contents (a glass tank). Keep the water volume comfortably below the wall height so it
  doesn't overflow.
- **For "an object drops into a still pool of water", use this recipe:** set top-level
  `settle_first:true` (lets the water come to rest before recording), and put
  `release_after_settle:true` on the falling object placed up in the air (it's held until the
  water is calm, then dropped). A floating object should be light (`density` ≈ 200). Without
  `settle_first` the water is still sloshing when the object arrives.
- **Particle bodies (liquid/sand/snow/elastic/cloth) can be thrown too.** `init_velocity`
  applies one uniform velocity to **every particle**, so a blob of water/sand/jelly/cloth can
  be launched as a whole — it flies as a parabola, then spreads/deforms on impact. Only
  `init_angular` is rigid-only (particles have no spin dof). A dropped blob just omits
  `init_velocity`.
- **Rest objects exactly ON their support — do NOT leave a gap (or they fall/float at t=0).**
  `pos` is the object's CENTER. To stand an object on a surface, its bottom must touch the
  surface top, so set the center height to `support_top_z + half_height_of_object`.
  - On the **ground floor** (always present at z=0): `pos_z = half_height`. A box of
    `size=[_,_,0.13]` has half-height 0.065, so `pos_z = 0.065`. A sphere: `pos_z = radius`.
  - On a **table/box** of `size=[_,_,t]` centered at `pos_z = c`: the table TOP is at
    `c + t/2`. An object of height `H` standing on it needs `pos_z = c + t/2 + H/2`.
    (Common mistake: putting the table center at z=0 with thickness 0.04 gives a top at z=0.02,
    not z=0 — objects placed at `pos_z = H/2` will be floating 0.02 m above it and drop at t=0.)
  - Verify before finalizing: object_bottom (`pos_z - H/2`) should EQUAL support_top, not exceed
    it (floating → falls) and not be below it (interpenetration → explosive push-out).
  - Tip: standing a row of items on the **ground floor** (`pos_z = half_height`) is simplest and
    avoids the table-top arithmetic entirely; add a table only when the scene needs one.
- **Set the initial state honestly.** `init_velocity` is the speed at t=0 — a thrown ball or
  tossed water blob gets a non-zero `init_velocity`; a dropped object gets `[0,0,0]` and falls
  under gravity. Mark every support/container `fixed: true`.
- **Use real units.** Positions and sizes in meters, velocities in m/s, density in kg/m³,
  gravity −9.81 m/s² on z unless the scenario is off-Earth. Scale matters: a 1 m drop and a
  100 m drop produce very different motion.
- **Clip length is fixed** at 3.0 s (24 fps) and is NOT settable — the tool always records a
  3-second clip (this keeps the reference valid for the downstream video generator). Set
  initial positions/velocities so the interesting motion happens within ~3 s (e.g. don't drop
  from 100 m and expect to see the landing).
- **The reference video is produced automatically** — the physics-correct clip is the point of
  this tool. The **trajectory** is returned alongside it; use it with `prompt_refine` (embed
  concrete positions/velocities into the caption) or with the verifier (check plausibility).
- **Only `color` (particle bodies) is yours to override.** Timestep, stability, and rendering
  are handled automatically — don't hand-tune them.

For per-material setup recipes and known limits, consult the material references:
[solver_rigid.md](solver_rigid.md), [solver_liquid.md](solver_liquid.md),
[solver_mpm.md](solver_mpm.md) (sand/snow/elastic), [solver_cloth.md](solver_cloth.md).

## Output

Returns `{trajectory, summary, dt, n_steps}` plus `video_path` (the reference clip — the main
deliverable), `render_fps`, and `n_rendered_frames`.

- `trajectory`: a map `{object_id: [state_per_step, ...]}`. Rigid objects report center
  position (and orientation); particle materials report the particle-cloud centroid.
- `summary`: one human-readable line describing the outcome.

Use `video_path` as the reference clip for the video generator; use `trajectory` to embed
concrete motion into the `video_prompt` (via `prompt_refine`) or to let the verifier check
physical plausibility.

## The reference clip is ABSTRACT — your `video_prompt` must paint the real scene

The rendered simulation shows **only the core objects** on a bare **checkerboard ground** with
an **empty/black background** — no environment, no real materials, no atmosphere. The video
generator uses this clip for its **motion** (trajectories, timing, deformation, collapse), NOT
its looks.

**CRITICAL — the reference clip LEAKS its look unless you override it.** The generator tends to
copy the reference's checkerboard floor and blank backdrop into the output *even when your
prompt mentions a different setting*, because a few scattered scene words don't outweigh what
it sees in the reference. A passing mention of "a wooden table in studio light" is NOT enough —
clips still come out with the grid floor. You must **actively and concretely build the entire
environment in the prompt so it overrides the reference**:

1. **Open by EXPLICITLY citing the clip as "Video 1".** seedance follows a reference video's
   motion only if the text names it; an attached-but-unnamed clip is largely ignored (the
   generator free-runs and drifts — wrong object count, broken motion). Start with seedance's
   Video Reference grammar: *"Reference Video 1's `<motion / timing / exact object count &
   layout>`, keeping `<that aspect>` consistent."* State exactly what to copy — for a physics
   clip that is the **motion/timing**, plus, if the scene has a countable number of objects,
   **the exact count and layout** ("the exact eight tiles in one row, same number and order").
2. **Then build the full environment that REPLACES the grid.** Right after the reference
   citation, establish the complete real scene — room/place, the surface the action happens on,
   the background, the floor ("a warm sunlit workshop, brick wall behind, wooden plank floor")
   — so there is a concrete thing to render instead of the checkerboard. Don't leave the
   floor/background unspecified; the reference's blank look leaks in otherwise.
3. **Specify materials, lighting, quality** — real texture of each object (grain of
   sand, wet sheen, wood grain), light direction/quality, shadows, dust; and quality words
   ("photorealistic", "cinematic", "4k"). Do NOT describe the camera, shot, or viewpoint
   (no "side view", "close-up", "low angle", no pan/zoom/orbit) — leave framing to the
   generator so the shot stays stable.
4. **Keep the motion aligned with the reference** (so the generator still follows the physics),
   but layer the full real-world appearance on top. Never contradict the clip (don't ask for a
   different count or action than it shows).

Do **not** name the simulator's abstract look in the prompt (don't write "checkerboard",
"particle cloud", "black background"); instead, crowd it out by fully describing the real scene
that should be there.

Template: *"Reference Video 1's <motion + exact object count/layout>, keeping it consistent. <full
real environment: place, surface, background, floor, lighting>. <object with real material>
<motion that matches the reference>. <quality words — no camera/viewpoint>."*

## Examples

Scenario: "A steel ball is dropped from 1 m onto a table and bounces." (Single contact
surface — drop onto the implicit ground floor; no table box. Call it a table in the
`video_prompt`.)

```json
{
  "objects": [
    {"id":"ball","type":"sphere","material":"rigid","pos":[0,0,1.0],"radius":0.04,"density":7800}
  ]
}
```

Scenario: "Water falls into a bowl." (Two surfaces — the bowl is a real container, so it IS
needed; it rests on the implicit ground floor, no separate table box. For a tossed stream of
water, add an `init_velocity` to the water blob — particle bodies honor it.)

```json
{
  "objects": [
    {"id":"water","type":"box","material":"liquid","pos":[0,0,0.45],"size":[0.1,0.1,0.12]},
    {"id":"bowl","type":"cylinder","material":"rigid","pos":[0,0,0.05],"radius":0.15,"height":0.1,"fixed":true}
  ]
}
```

Scenario: "A rubber duck drops into a still tank of water and bobs." (Glass tank from fixed
transparent walls, water pre-settled to a calm smooth surface, duck held in the air then
released into it.)

```json
{
  "settle_first": true,
  "objects": [
    {"id":"water","type":"box","material":"liquid","pos":[0,0,0.21],"size":[0.86,0.86,0.40],"color":[0.2,0.6,1.0]},
    {"id":"base","type":"box","material":"rigid","pos":[0,0,0.0],"size":[0.9,0.9,0.04],"fixed":true,"transparent":true},
    {"id":"wx+","type":"box","material":"rigid","pos":[0.45,0,0.25],"size":[0.04,0.9,0.5],"fixed":true,"transparent":true},
    {"id":"wx-","type":"box","material":"rigid","pos":[-0.45,0,0.25],"size":[0.04,0.9,0.5],"fixed":true,"transparent":true},
    {"id":"wy+","type":"box","material":"rigid","pos":[0,0.45,0.25],"size":[0.9,0.04,0.5],"fixed":true,"transparent":true},
    {"id":"wy-","type":"box","material":"rigid","pos":[0,-0.45,0.25],"size":[0.9,0.04,0.5],"fixed":true,"transparent":true},
    {"id":"duck","type":"mesh","asset":"duck","material":"elastic","pos":[0,0,0.95],"euler":[90,0,90],"density":200,"color":[0.95,0.82,0.1],"release_after_settle":true}
  ]
}
```

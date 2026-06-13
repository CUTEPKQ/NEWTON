---
name: solver_mpm
description: Reference for the simulator skill — the materials sand, snow, elastic, and elastoplastic. Consulted via simulator.md when building a scene_spec with granular piles, snow, springy soft bodies, or putty/clay that dents permanently; not selected on its own.
---

# solver_mpm (sand / snow / elastic / elastoplastic)

Reference companion to [simulator.md](simulator.md) for the four deformable
materials — `sand`, `snow`, `elastic`, `elastoplastic`. Read simulator.md first.

These represent a body as a **cloud of particles**. Use them for materials that
deform or flow but are not a simple liquid:
- **`sand`** — granular flow, collapsing/pouring piles, dunes.
- **`snow`** — packing granular that clumps (snowball, snow pile).
- **`elastic`** — soft solids that **fully spring back** to their original shape
  (jelly, rubber ball, bouncy soft toy). Deforms on impact, then rebounds. The
  deformation is 100% recoverable: it stores ALL the impact energy as elastic
  strain and gives it back. Pick this ONLY when the object should end up looking
  exactly like it started (no permanent dent).
- **`elastoplastic`** — putty that **dents permanently and does NOT spring back**
  (plasticine / modelling clay / dough / a clay figurine). On impact it squashes
  and *keeps* the squashed shape. **This is the right material whenever the
  scenario implies the object stays deformed** — "a clay duck dropped on a table",
  "a ball of dough flattens", "a putty figure splats". Do not use `elastic` for
  these (it would bounce back like rubber, which is wrong).

**Choosing between them — the one test:** *after the motion settles, is the object
back to its original shape (→ `elastic`) or does it stay deformed (→
`elastoplastic`)?* Physically, `elastoplastic` = `elastic` PLUS a yield threshold
(`yield_stress`): strain below the threshold springs back, strain above it becomes
a permanent set. So `elastoplastic` BOTH bounces a little AND keeps a dent, while
`elastic` only bounces.

**Caveat — `elastic` has no damping, so it never stops.** A pure `elastic` body
has no energy loss: a dropped elastic ball compresses, rebounds, and then keeps
bouncing/jiggling for the entire clip (it never comes to rest — physically wrong
for anything that should settle). If you want a soft body that bounces a few times
and then settles, use `elastoplastic` with a HIGH `yield_stress` (mostly elastic
but it bleeds off energy each impact and damps out). Reserve pure `elastic` for
genuinely never-resting jiggle (a wobbling jelly held in frame briefly).

The simulator handles stability and timestep automatically. For elastic /
elastoplastic you MAY optionally tune softness — see Level 2.

## Level 1 — minimal: a soft/granular blob dropped
The body is a `box` volume filled with particles. Start it **above** the ground.
```json
{
  "objects": [
    {"id":"blob","type":"box","material":"elastic","pos":[0,0,0.4],"size":[0.12,0.12,0.12]}
  ],
  "duration": 1.0
}
```
Resting behavior (started at z 0.4): `elastic` settles ~0.17 (squashes, holds
height, springs); `sand` collapses to a flat spread (~0); `snow` packs into a low
mound (~0.07). Swap `"material"` among the three — same scene shape.

## Level 2 — parameters
| field | meaning | notes |
|---|---|---|
| `type` | **`box`** for a blob, or **`mesh`** for a shaped figure (duck/dragon/bunny) | a box defines the initial particle volume |
| `size` | [x,y,z] m | **box only**: the blob's starting size; ≤ ~0.2 m/side for speed |
| `scale` | — | **do NOT set for a soft/MPM mesh**: shaped meshes are auto-sized to a fixed, clearly-visible size (the built-in assets differ wildly in raw size, so a manual scale is unreliable) |
| `pos` | center of the volume | **must start above the floor** (box bottom > 0) |
| `density` | kg/m³, default 1000 | rarely changed |
| `stiffness` | elastic/elastoplastic only: Young's modulus E (default 3e5) | **lower = softer**. ~3e4 very soft/wobbly, 3e5 firm. |
| `yield_stress` | **elastoplastic only**: von Mises yield stress (default 6e3) | **lower = dents more easily**. 6e3 (default) good soft clay; <1e3 collapses into a puddle; ≥1e4 barely deforms. |
| `fixed` | leave **false** | the ground/container is the `fixed:true` rigid object |

### Tuning elastic vs elastoplastic (important)
- **`elastic` springs back; `elastoplastic` stays dented.** Pick by what the
  scenario implies, not by stiffness. A clay/putty/dough object → `elastoplastic`.
- **For clay/putty, the key knob is `yield_stress`, NOT `stiffness`.** Lower
  `yield_stress` = yields (deforms permanently) more easily. A good plasticine
  look is **`yield_stress` ≈ 6000** — which is the DEFAULT, so you usually need not
  set it at all (firm clay that clearly dents on impact but keeps the figure
  recognizable). Going much lower (≤1000) makes it slump into a flat blob; ≥1e4
  barely deforms (looks rigid). Counter-intuitively,
  lowering `stiffness` (E) alone does NOT make it dent — it just makes it wobble;
  permanent denting is governed by `yield_stress`.
- **For jelly/rubber (`elastic`), use `stiffness`.** Lower E (e.g. 3e4) = softer,
  bigger wobble and rebound; higher E = stiffer bounce.

## Level 3 — tuning notes & honest limits
- **Initial velocity IS applied.** `init_velocity` sets one uniform velocity on
  every particle, so a blob of sand/snow/jelly can be **thrown** — it flies as a
  parabola and then spreads/collapses on impact (sand scatters, jelly squashes and
  rebounds). There is no angular velocity for particle bodies (`init_angular` is
  ignored); arrange spin/tumble with rigid bodies instead.
- **Start above the ground.** A body that starts intersecting the floor gets a
  violent contact response. Place `pos.z` so the box bottom is clearly > 0.
- **Keep `duration` short and volumes small** — these are the slowest materials.
- **Behavior is qualitative, not metrologically exact.** Use it to show *the kind*
  of motion (sand collapses, jelly wobbles, snow packs), not precise numbers.

## How it renders
- **`elastic` / `elastoplastic`** render as a **smooth colored solid** (elastic red,
  elastoplastic yellow by default) — you see one coherent body squash; elastic
  rebounds, elastoplastic keeps the dent.
- **`sand` / `snow`** render as a **colored particle cloud** (sand = tan, snow =
  white) — correct for granular spray/piling.
Override any of them with a per-object `"color":[r,g,b]` (0–1).

## What MPM gives you back
Per step, the **centroid of the particle cloud**. Enough to describe bulk motion
("the sand pile collapses and spreads", "the jelly cube squashes and rebounds").
The reference video shows the visible deformation.

## Examples
A jelly cube dropped and wobbling:
```json
{
  "objects": [
    {"id":"jelly","type":"box","material":"elastic","pos":[0,0,0.5],"size":[0.15,0.15,0.15]}
  ],
  "duration": 1.5
}
```

A sand pile collapsing onto a table (single surface — drop onto the implicit ground
floor, no table box; particle bodies would punch through a thin slab anyway):
```json
{
  "objects": [
    {"id":"sand","type":"box","material":"sand","pos":[0,0,0.3],"size":[0.2,0.2,0.2]}
  ],
  "duration": 1.5
}
```

A clay (plasticine) duck dropped on a table — squashes and KEEPS the dent. Defaults
already give a firm-clay look (`elastoplastic` → E 3e5, `yield_stress` 6e3), and a
soft/granular mesh is AUTO-SIZED, so you set NO `scale`, `stiffness`, or
`yield_stress` — just the asset, a drop height, orientation, and color. Single
surface, so it drops onto the implicit ground floor (no table box); call it a table
in the `video_prompt`:
```json
{
  "objects": [
    {"id":"clay_duck","type":"mesh","asset":"duck","material":"elastoplastic","pos":[0,0,0.5],"euler":[90,0,0],"density":400,"color":[0.95,0.82,0.1]}
  ]
}
```

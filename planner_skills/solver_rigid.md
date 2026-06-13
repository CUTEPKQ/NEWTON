---
name: solver_rigid
description: Reference for the simulator skill — the rigid material. Consulted via simulator.md when building a scene_spec with rigid bodies (balls, blocks, tables, cups, ground); not selected on its own.
---

# solver_rigid

Reference companion to [simulator.md](simulator.md) for the **rigid** material.
Read simulator.md first for the overall `scene_spec` shape; this file covers how
to set up rigid bodies well and which knobs actually move the result.

`rigid` is for solid objects that keep their shape: balls, blocks, tools, tables,
cups, the ground. It is the default material. Use it for everything that is not a
fluid, granular pile, soft body, or cloth.

## Level 1 — minimal: drop and rest
The smallest useful rigid scene is one moving body + a fixed ground.
```json
{
  "objects": [
    {"id":"ball","type":"sphere","material":"rigid","pos":[0,0,1.0],"radius":0.05,"density":1000}
  ],
  "duration": 1.0
}
```
The ball free-falls and **rests with its center at z = radius**. That resting
height = radius is your sanity check that contact worked.

## Level 2 — the parameters that matter
| field | meaning | effect (verified) |
|---|---|---|
| `pos` | initial center [x,y,z] m | where it starts; set z so the body starts **above** the ground (z > radius, or box bottom > 0) |
| `radius` / `size` | sphere radius / box size, m | geometry; resting height follows from it |
| `density` | kg/m³ (wood≈600, water≈1000, steel≈7800) | mass = density × volume; affects momentum in collisions, **not** fall speed (gravity is mass-independent) |
| `init_velocity` | [vx,vy,vz] m/s at t=0 | a thrown/launched body; horizontal vx makes it travel then land |
| `init_angular` | [wx,wy,wz] rad/s at t=0 | initial spin; use for toppling/tumbling |
| `fixed` | bool | `true` = immovable support (ground, table, container walls) |

### Initial state recipes
- **Drop:** `init_velocity` omitted (or [0,0,0]); place `pos.z` above rest height.
- **Throw / projectile:** `init_velocity:[vx,0,vz]` with vz>0 for an arc, vx for range.
- **Topple a standing box:** give a small `init_angular:[0,ω,0]` (ω≈2 rad/s) or
  start it slightly tilted via a leaning `pos`; verified a box with init_angular
  tips and falls.
- **Slide / roll:** horizontal `init_velocity:[vx,0,0]`; verified vx=2 m/s carries
  the body ~1.2 m before stopping.

## Level 3 — tuning notes & honest limits
- **Surface friction is "roughly on", not a precise dial** — don't count on exact
  slide/roll distances.
- **Multiple bodies / stacking** work: place each with a distinct `pos`, mark
  supports `fixed:true`. Collisions resolve correctly.

## How it renders
Rigid bodies show as **solid lit shapes**. They use the default surface — you do
not set a color for them (only particle materials take a `color`).

## What rigid gives you back
Per step, each non-fixed rigid body reports its **center position** (and
orientation). The trajectory is exact and clean — ideal for embedding concrete
positions/velocities into the caption.

## Examples
Thrown ball landing on a table (single surface — land on the implicit ground floor, no
table box; call it a table in the `video_prompt`):
```json
{
  "objects": [
    {"id":"ball","type":"sphere","material":"rigid","pos":[-0.5,0,0.4],"radius":0.05,"density":1000,"init_velocity":[1.5,0,1.0]}
  ],
  "duration": 1.5
}
```

Box toppling off an edge:
```json
{
  "objects": [
    {"id":"box","type":"box","material":"rigid","pos":[0,0,0.6],"size":[0.2,0.2,0.4],"density":600,"init_angular":[0,2.0,0]}
  ],
  "duration": 1.5
}
```

---
name: solver_cloth
description: Reference for the simulator skill — the cloth material. Consulted via simulator.md when building a scene_spec with fabric, flags, tablecloths, curtains, or draping covers; not selected on its own.
---

# solver_cloth

Reference companion to [simulator.md](simulator.md) for the **cloth** material.
Read simulator.md first.

`cloth` is for thin flexible sheets: fabric, flags, tablecloths, curtains,
draping covers. It is represented as a grid of linked particles.

## Level 1 — minimal: a sheet draping onto the floor
Model the sheet as a **thin, wide `box`** (a flat slab) and drop it.
```json
{
  "objects": [
    {"id":"sheet","type":"box","material":"cloth","pos":[0,0,0.6],"size":[0.3,0.3,0.02]}
  ],
  "duration": 1.2
}
```
The sheet falls and **flattens onto the floor** (centroid ends near z≈0, i.e. it
lies flat).

## Level 2 — parameters
| field | meaning | notes |
|---|---|---|
| `type` | use **`box`** | a flat slab represents the sheet |
| `size` | [width, depth, **thickness**] m | make it **wide and thin**: large x,y, small z (≈0.01–0.02) |
| `pos` | center of the sheet | start it above whatever it drapes onto |
| `fixed` | leave **false** | the floor / object it drapes over is the `fixed:true` rigid body |

Cloth physical constants use defaults that give natural fabric drape; you do not
set them from `scene_spec`.

## Level 3 — tuning notes & honest limits
- **Make it a thin slab.** Width/depth ≫ thickness. A 0.3×0.3×0.02 m sheet
  behaves like fabric; a cube-ish box will not drape.
- **Draping over an object works** but is imperfect: a sheet dropped over a tall
  fixed box may catch and partly slide off. For a clean "cloth covering a table"
  shot, size the sheet a bit larger than the object top and keep `duration` long
  enough (≥1.5 s) for it to settle.
- **Initial velocity IS applied** to cloth — `init_velocity` sets one uniform
  velocity on every particle, so the sheet can be **tossed** (a flag thrown into
  the air, a cloth flung sideways) and then drapes/settles. There is no angular
  velocity for the particles (`init_angular` ignored). A plain drop with no
  `init_velocity` is the common case.

## How it renders
The sheet shows as a **smooth colored surface** (pink-ish by default) — a
continuous piece of fabric, not a particle cloud. Override with a per-object
`"color":[r,g,b]` (0–1).

## What cloth gives you back
Per step, the **centroid of the cloth particles**. Enough to state the bulk
motion ("the cloth falls and drapes over the table"); the reference video shows
the actual folds/drape shape.

## Example
A tablecloth dropping over a table:
```json
{
  "objects": [
    {"id":"sheet","type":"box","material":"cloth","pos":[0,0,0.8],"size":[0.5,0.5,0.02]},
    {"id":"table","type":"box","material":"rigid","pos":[0,0,0.4],"size":[0.4,0.4,0.05],"fixed":true}
  ],
  "duration": 1.8
}
```

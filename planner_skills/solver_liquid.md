---
name: solver_liquid
description: Reference for the simulator skill — the liquid material. Consulted via simulator.md when building a scene_spec with water/juice/oil or any free-flowing fluid; not selected on its own.
---

# solver_liquid

Reference companion to [simulator.md](simulator.md) for the **liquid** material.
Read simulator.md first.

`liquid` is for free-flowing fluids: water, juice, oil, any pourable/splashing
liquid. The fluid is represented as a **cloud of particles**.

## Level 1 — minimal: a blob of water falling
A fluid body is a `box` whose volume is **filled with particles** at build time.
```json
{
  "objects": [
    {"id":"water","type":"box","material":"liquid","pos":[0,0,0.5],"size":[0.12,0.12,0.12]}
  ],
  "duration": 1.0
}
```
The blob falls, hits the ground, and **spreads flat** (centroid ends near z≈0,
i.e. it pooled on the floor).

## Level 2 — parameters
| field | meaning | notes |
|---|---|---|
| `type` | use **`box`** | the box defines the **initial liquid volume**; particles fill it. (A sphere also works but box is the natural "body of water".) |
| `size` | [x,y,z] m of the starting volume | bigger volume = more particles = slower but more detailed. Keep ≤ ~0.25 m per side for speed. |
| `pos` | center of that volume | start it **above** the floor/container so it falls in. |
| `density` | kg/m³, default 1000 (water) | rarely needs changing for water. |
| `fixed` | leave **false** for the liquid | the container/floor it pours onto is the `fixed:true` rigid object. |

Fluid behavior uses water-like defaults automatically; you do not set physical
constants from `scene_spec`.

## Level 3 — tuning notes & honest limits
- **Initial velocity IS applied to liquid.** `init_velocity` sets one uniform
  velocity on every fluid particle, so a blob of water can be **thrown / launched
  sideways** — it flies as a parabola and then spreads on impact. Use this for a
  tossed volume of water or a sideways jet. There is no angular velocity for
  fluids (`init_angular` is ignored). For a gentle pour you can also just drop the
  volume above an offset target and let gravity carry it.
- **Container:** model cups/bowls as `fixed:true` rigid `box`/`cylinder` walls.
  The fluid will pool inside them.
- **Keep volumes modest** (≤ ~0.25 m/side): bigger = more particles = slower.
- **Stability check:** if the summary shows the centroid z shooting upward or to a
  huge value, shrink the volume `size`.

## How it renders
The fluid shows as a **blue particle cloud** (water-like color by default).
Override with a per-object `"color":[r,g,b]` (0–1) for juice, oil, etc.

## What liquid gives you back
Per step the fluid reports the **centroid of its particle cloud** (mean particle
position). That is enough to describe the bulk motion ("water falls and spreads")
in the caption; the reference video shows the actual splash/pour.

## Example
Water poured from above into a bowl (drop-in pour; for a tossed stream add an
`init_velocity` to the water). The bowl is a real container so it IS needed; it rests on
the implicit ground floor — no separate table box (call it a table in the `video_prompt`):
```json
{
  "objects": [
    {"id":"water","type":"box","material":"liquid","pos":[0,0,0.45],"size":[0.1,0.1,0.12]},
    {"id":"bowl","type":"cylinder","material":"rigid","pos":[0,0,0.05],"radius":0.15,"height":0.1,"fixed":true}
  ],
  "duration": 1.5
}
```

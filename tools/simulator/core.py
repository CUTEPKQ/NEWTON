"""Self-contained Genesis simulation core for the OpenNewton `simulator` tool.

Translates a structured ``scene_spec`` (see planner_skills/simulator.md) into a
Genesis scene, runs the physics, and returns per-object trajectories. Optionally
renders a physics-correct reference video.

No LLM: the planner has already turned the natural-language scenario into the
structured ``scene_spec``; this module is a deterministic translation layer.

scene_spec (abridged):
    {
      "objects": [
        {"id","type","material","pos","radius"|"size","density",
         "init_velocity","init_angular","fixed"}, ...
      ],
      "gravity": [0,0,-9.81], "duration": 3.0, "dt": 0.01, "render": false
    }

material -> solver routing (Genesis: the material picks the solver):
    rigid                 -> Rigid solver        (RigidEntity, get_pos/get_quat)
    liquid                -> SPH solver          (particles, get_particles_pos)
    sand | snow | elastic -> MPM solver          (particles, get_particles_pos)
    cloth                 -> PBD solver          (particles, get_particles_pos)
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

# Genesis is heavy and pins global state via gs.init(); import lazily.
_GS = None
_GS_INITED = False


# Which Genesis solver each scene_spec material maps to. The material class
# itself routes the entity to its solver; we only need this to decide which
# solver *options* the Scene must enable and how to sample state afterwards.
_PARTICLE_MATERIALS = {"liquid", "sand", "snow", "elastic", "elastoplastic", "cloth"}

# Default Young's modulus for elastic/elastoplastic when the planner does not set
# `stiffness`. We MUST set this explicitly: Genesis' own default is E=1e6, which at
# the fixed MPM mesh scale (_MPM_MESH_SCALE) is too stiff and blows up on impact
# (the body freezes mid-air / explodes into debris). 3e5 is the stable, firm-but-
# soft value the skill docs advertise as the default.
_DEFAULT_MPM_E = 3e5
# Default von Mises yield stress for elastoplastic when the planner does not set
# `yield_stress`. Genesis' own default is 1e4, which barely yields (the clay lands
# almost rigid, no visible dent). 6e3 is the firm-clay value the skill recommends:
# it clearly squashes and KEEPS the dent without slumping into a puddle.
_DEFAULT_MPM_YIELD = 6e3
_MATERIAL_SOLVER = {
    "rigid": "rigid",
    "liquid": "sph",
    "sand": "mpm",
    "snow": "mpm",
    "elastic": "mpm",
    "elastoplastic": "mpm",
    "cloth": "pbd",
}

# Reference-clip timing is fixed (not planner-controllable). The clip feeds a
# downstream video generator that requires a 1.8-15.2s reference; a fixed
# 3.0s @ 24fps clip is always valid and keeps the tool surface simple.
#
# SLOW MOTION: the physics is only simulated for SIM_PHYS_DURATION_S seconds (the
# active part of the motion — fall, impact, deformation, collapse), but those
# frames are played back over the full SIM_DURATION_S at SIM_RENDER_FPS. With
# 1.0s of physics shown over 3.0s of playback this is a 3x slow-motion clip:
# real impacts (kept at true gravity) are stretched out so the deformation reads
# clearly, and there is no dead static tail. Slow-mo factor = SIM_DURATION_S /
# SIM_PHYS_DURATION_S.
SIM_DURATION_S = 3.0       # playback length of the clip (seconds)
SIM_RENDER_FPS = 24.0      # playback frame rate -> 72 frames total
SIM_PHYS_DURATION_S = 1.0  # how much real physics time is captured (then slowed)


class SimCore:
    """Build + run a Genesis simulation from a scene_spec."""

    def __init__(self, backend: str = "gpu", output_dir: Optional[str] = None):
        self.backend = backend
        self.output_dir = output_dir or os.environ.get(
            "SIMULATOR_OUTPUT_DIR",
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "outputs", "sim"),
        )
        os.makedirs(self.output_dir, exist_ok=True)

    # -- Genesis init (once per process) ------------------------------------
    def _ensure_gs(self):
        global _GS, _GS_INITED
        if _GS is None:
            import genesis as gs
            _GS = gs
        if not _GS_INITED:
            backend = _GS.gpu if self.backend == "gpu" else _GS.cpu
            _GS.init(backend=backend)
            _GS_INITED = True
        return _GS

    # -- morph construction --------------------------------------------------
    def _make_morph(self, gs, o: Dict[str, Any]):
        t = o["type"]
        pos = tuple(o.get("pos", [0.0, 0.0, 0.0]))
        # optional orientation in degrees (extrinsic x-y-z); used e.g. for ramps
        opts: Dict[str, Any] = {}
        euler = o.get("euler")
        if euler is not None:
            opts["euler"] = tuple(euler)
        # fixed=True must be passed to the MORPH to actually pin the body in the
        # world (e.g. a ramp/table/container). Without it the body still falls
        # under gravity -- the spec-level "fixed" was previously only used to skip
        # initial-velocity, not to anchor the geometry. Plane is always fixed.
        if o.get("fixed", False):
            opts["fixed"] = True
        if t == "sphere":
            return gs.morphs.Sphere(pos=pos, radius=float(o.get("radius", 0.5)), **opts)
        if t == "box":
            return gs.morphs.Box(pos=pos, size=tuple(o.get("size", [0.1, 0.1, 0.1])), **opts)
        if t == "cylinder":
            return gs.morphs.Cylinder(
                pos=pos, radius=float(o.get("radius", 0.1)),
                height=float(o.get("height", o.get("size", [0, 0, 0.1])[-1])), **opts,
            )
        if t == "plane":
            return gs.morphs.Plane()
        if t == "mesh":
            asset = o.get("asset")
            if asset not in self._MESH_ASSETS:
                raise ValueError(
                    f"unknown mesh asset {asset!r}; choose one of {sorted(self._MESH_ASSETS)}")
            return gs.morphs.Mesh(file=self._MESH_ASSETS[asset], pos=pos,
                                  scale=self._mesh_scale(o), **opts)
        raise ValueError(f"unknown object type: {t!r}")

    # MPM particle meshes are AUTO-SIZED to a fixed target world size. At
    # grid_density=64 (dx~0.0156 m) too SMALL a mesh (<~0.2 m) spans too few
    # particles and loses its shape (a detailed dragon reads as a sparse blob);
    # 0.30 m gives a crisp silhouette AND a clean plastic squash on impact, and
    # keeps the body (even rotated) well inside the MPM solver domain. CRUCIAL: the
    # built-in OBJ assets are NOT unit-sized — duck is ~4 units tall, dragon ~1 — so
    # a single scale FACTOR cannot give a consistent physical size across assets. We
    # pin the longest world edge to _MPM_MESH_TARGET_SIZE and back out each asset's
    # scale from its own raw extent, so every soft mesh lands at the same prominent,
    # stable size regardless of asset. The planner does not control MPM mesh size.
    # Rigid meshes keep their own `scale` (a solid body renders fine at any size;
    # only particle sampling is the issue). box/sphere MPM bodies are left to the
    # planner's `size`/`radius`: they are shape-stable at every size and their
    # extent is physically meaningful (a sand pile's footprint, a jelly cube).
    _MPM_MESH_TARGET_SIZE = 0.30

    @classmethod
    def _mesh_scale(cls, o: Dict[str, Any]) -> float:
        """Scale for a mesh body. Rigid meshes use the planner's `scale` (default
        1.0). MPM-particle meshes are auto-sized: longest raw edge -> target size,
        ignoring any planner `scale` (the assets differ ~9x in raw size, so a fixed
        factor cannot give a consistent, stable physical size)."""
        if o.get("material", "rigid") not in _PARTICLE_MATERIALS:
            return float(o.get("scale", 1.0))
        longest = max(cls._MESH_RAW_SIZE.get(str(o.get("asset")), (1.0,)))
        return cls._MPM_MESH_TARGET_SIZE / longest

    # Genesis ships these meshes in its asset library; the planner references them
    # by name (no file import). Maps a friendly name -> bundled file path.
    _MESH_ASSETS = {
        "duck": "meshes/duck.obj",
        "bunny": "meshes/bunny.obj",
        "dragon": "meshes/dragon.obj",
        "sphere": "meshes/sphere.obj",
    }

    # Raw (unscaled) AABB edge lengths of each built-in mesh, measured from the OBJ
    # vertices. Used to auto-size MPM meshes to a consistent world size (see
    # _mesh_scale): the assets are very different raw sizes (duck ~4u, dragon ~1u),
    # so we normalize by the true extent rather than a blind scale factor.
    _MESH_RAW_SIZE = {
        "duck": (2.404, 2.512, 3.978),
        "bunny": (1.0, 0.774, 0.989),
        "dragon": (1.0, 0.707, 0.447),
        "sphere": (2.0, 2.0, 2.0),
    }

    # -- material construction ----------------------------------------------
    def _make_material(self, gs, o: Dict[str, Any]):
        mat = o.get("material", "rigid")
        rho = o.get("density")
        if mat == "rigid":
            return gs.materials.Rigid(rho=rho) if rho else gs.materials.Rigid()
        if mat == "liquid":
            return gs.materials.SPH.Liquid(rho=rho) if rho else gs.materials.SPH.Liquid()
        if mat == "sand":
            return gs.materials.MPM.Sand(rho=rho) if rho else gs.materials.MPM.Sand()
        if mat == "snow":
            return gs.materials.MPM.Snow(rho=rho) if rho else gs.materials.MPM.Snow()
        if mat == "elastic":
            # E = Young's modulus (stiffness): lower = softer/wobblier. Default to
            # _DEFAULT_MPM_E (NOT Genesis' 1e6, which is unstable at our mesh scale).
            kw: Dict[str, Any] = {}
            if rho:
                kw["rho"] = rho
            kw["E"] = float(o["stiffness"]) if o.get("stiffness") is not None else _DEFAULT_MPM_E
            return gs.materials.MPM.Elastic(**kw)
        if mat == "elastoplastic":
            # Plastic deformation (permanent dent, no full rebound). The two knobs:
            # E (stiffness) and von_mises_yield_stress. The yield THRESHOLD is
            # yield_stress/(2*mu) with mu proportional to E, so the permanent-dent
            # behavior is governed by the yield_stress/E ratio (plus E must not be
            # too high or the body just bounces). For putty/plasticine/dough.
            kw = {}
            if rho:
                kw["rho"] = rho
            kw["E"] = float(o["stiffness"]) if o.get("stiffness") is not None else _DEFAULT_MPM_E
            kw["von_mises_yield_stress"] = (
                float(o["yield_stress"]) if o.get("yield_stress") is not None else _DEFAULT_MPM_YIELD
            )
            return gs.materials.MPM.ElastoPlastic(**kw)
        if mat == "cloth":
            return gs.materials.PBD.Cloth()
        raise ValueError(f"unknown material: {mat!r}")

    # -- surface (visual) construction --------------------------------------
    _DEFAULT_COLORS = {
        "liquid": (0.2, 0.5, 0.95),
        "sand": (0.85, 0.72, 0.45),
        "snow": (0.95, 0.95, 1.0),
        "elastic": (0.9, 0.25, 0.25),
        "elastoplastic": (0.95, 0.82, 0.1),
        "cloth": (0.85, 0.3, 0.55),
        "rigid": (0.8, 0.3, 0.3),
    }

    # vis_mode per material. Bodies that hold a coherent shape (a jelly cube, a
    # cloth sheet) look far more realistic as a SMOOTH SURFACE ("visual"); bodies
    # that fragment into a spray/pile (water, sand, snow) read correctly as a
    # PARTICLE cloud. (The Genesis MPM/PBD tutorials use exactly this split.)
    _VIS_MODE = {
        "elastic": "visual",
        "elastoplastic": "visual",
        "cloth": "visual",
        "liquid": "particle",
        "sand": "particle",
        "snow": "particle",
    }

    def _make_surface(self, gs, o: Dict[str, Any]):
        """Build the visual surface for an object.

        - PARTICLE materials (SPH/MPM/PBD) MUST get a vis_mode or they render
          nothing (a vis_mode=None body adds no visual node -> black video).
          elastic/cloth -> smooth surface ('visual'); liquid/sand/snow ->
          particle cloud.
        - RIGID bodies render fine with a plain Default(color=...) surface. (An
          earlier note here claimed a surface turned rigid bodies black; that was
          GPU-starvation black frames, not the surface -- verified a colored rigid
          box renders correctly.) A rigid body with `transparent:true` becomes
          SEE-THROUGH via alpha blending (`opacity` sets the alpha), so you can use
          it as a glass-like container whose contents stay visible. NOTE: true glass
          refraction needs a ray tracer; our rasterizer does alpha blending only,
          which still reads as a clear container. (gs.surfaces.Glass renders as an
          opaque block under the rasterizer, so we don't use it.)
        - The Plane keeps Genesis' own (checker) handling: return None for it.
        """
        t = o.get("type")
        mat = o.get("material", "rigid")
        if t == "plane":
            return None
        if mat in _PARTICLE_MATERIALS:
            color = tuple(o.get("color", self._DEFAULT_COLORS.get(mat, (0.8, 0.3, 0.3))))
            # vis_mode is HARD-CODED per material (see _VIS_MODE) and not
            # planner-overridable: each material only supports certain modes
            # (e.g. Genesis Sand accepts only 'particle'/'recon', never 'visual'),
            # and the right look for each is fixed — sand/liquid/snow as a particle
            # cloud, elastic/elastoplastic/cloth as a smooth skinned surface.
            vis_mode = self._VIS_MODE.get(mat, "particle")
            return gs.surfaces.Default(color=color, vis_mode=vis_mode)
        # rigid (non-plane). Transparency is opt-in ONLY via an explicit
        # `transparent:true`; `opacity` is just the alpha used when transparent.
        # (Planners often add a stray `opacity` to opaque bodies — keying off its
        # mere presence would wrongly render solid balls/blocks see-through.)
        transparent = bool(o.get("transparent"))
        if transparent:
            color = tuple(o.get("color", (0.7, 0.85, 1.0)))
            opacity = float(o.get("opacity", 0.3))
            return gs.surfaces.Default(color=color, opacity=opacity)
        if "color" in o:
            return gs.surfaces.Default(color=tuple(o["color"]))
        return None  # default rigid look

    @staticmethod
    def _obj_half_extent(o: Dict[str, Any]) -> "np.ndarray":
        """Rough half-size of an object for domain/camera bounds. box->size/2,
        sphere/cylinder->radius, mesh->(raw AABB * effective scale). Uses the SAME
        effective mesh scale as _morph (auto-sized for MPM meshes), applied to the
        asset's REAL per-axis extent (the assets are very non-cubic, e.g. the duck
        is ~4 units tall but ~2.4 wide), so the domain/camera enclose what is
        actually built. Using a cube of side=scale here would under-size the box and
        let rotated particles leave the MPM solver boundary."""
        if o.get("type") == "mesh":
            s = SimCore._mesh_scale(o)
            raw = SimCore._MESH_RAW_SIZE.get(str(o.get("asset")), (1.0, 1.0, 1.0))
            return 0.5 * np.array(raw, dtype=float) * s
        return 0.5 * np.array(o.get("size", [o.get("radius", 0.1)] * 3), dtype=float)

    @staticmethod
    def _snap_to_support(spec: Dict[str, Any]) -> None:
        """Fix small accidental gaps so objects MEANT to rest on a surface start
        touching it, in place (mutates each object's pos z).

        The planner frequently miscomputes the standing height by a centimeter or
        two (e.g. table top at 0.40, tiles placed with bottom at 0.42), so the
        bodies start floating and free-fall at t=0 instead of resting. We close
        ONLY that kind of tiny gap, and ONLY for bodies that are clearly meant to
        rest, never for objects that are supposed to be airborne:

          - gap to the nearest support below must be small (< 50% of the object's
            own height) — a real drop from height has a large gap and is skipped;
          - no downward initial velocity (vz >= 0) — thrown/dropped bodies keep
            their placement;
          - not release_after_settle (those are intentionally held in the air).

        Support surfaces are fixed bodies whose horizontal footprint overlaps the
        object: a ground plane (top z=0) or a fixed box (top = pos_z + size_z/2).
        """
        objs = spec.get("objects", [])
        # collect fixed supports as (top_z, (xlo,xhi,ylo,yhi) or None for plane)
        supports = []
        for o in objs:
            if not o.get("fixed", False):
                continue
            t = o.get("type")
            if t == "plane":
                supports.append((0.0, None))
            elif t in ("box", "cylinder"):
                pos = o.get("pos", [0, 0, 0])
                half = SimCore._obj_half_extent(o)
                top = float(pos[2]) + float(half[2])
                fp = (pos[0] - half[0], pos[0] + half[0], pos[1] - half[1], pos[1] + half[1])
                supports.append((top, fp))
        if not supports:
            return
        for o in objs:
            if o.get("fixed", False) or o.get("type") == "plane":
                continue
            if o.get("release_after_settle", False):
                continue
            vz = (o.get("init_velocity") or [0, 0, 0])
            if len(vz) >= 3 and float(vz[2]) < 0:
                continue
            pos = o.get("pos")
            if not pos or len(pos) < 3:
                continue
            half = SimCore._obj_half_extent(o)
            obj_h = float(half[2]) * 2.0
            bottom = float(pos[2]) - float(half[2])
            # highest support directly under this object's footprint
            best_top = None
            for top, fp in supports:
                if fp is not None:
                    if not (fp[0] <= pos[0] <= fp[1] and fp[2] <= pos[1] <= fp[3]):
                        continue
                if top <= bottom + 1e-9:  # support is at/below the object bottom
                    if best_top is None or top > best_top:
                        best_top = top
            if best_top is None:
                continue
            gap = bottom - best_top
            if 0 < gap < 0.5 * obj_h:
                o["pos"] = [float(pos[0]), float(pos[1]), best_top + float(half[2])]

    # -- solver-option inference --------------------------------------------
    def _bounds(self, spec: Dict[str, Any]) -> Tuple[tuple, tuple]:
        """Axis-aligned domain that encloses every object plus margin.
        SPH/MPM require an explicit simulation box."""
        pts = []
        for o in spec["objects"]:
            if o.get("type") == "plane":
                continue
            p = np.array(o.get("pos", [0, 0, 0]), dtype=float)
            half = self._obj_half_extent(o)
            pts.append(p - half)
            pts.append(p + half)
        if not pts:
            return (-1.0, -1.0, 0.0), (1.0, 1.0, 1.0)
        lo = np.min(pts, axis=0) - 0.5
        hi = np.max(pts, axis=0) + 0.5
        lo[2] = min(lo[2], 0.0)  # include the ground plane
        return tuple(lo.tolist()), tuple(hi.tolist())

    def _frame_camera(self, spec, res, aabb) -> Tuple[tuple, tuple, float]:
        """Camera that frames the axis-aligned box the objects sweep through over
        the WHOLE clip, so nothing leaves the frame in later steps.

        `aabb` is ((xlo,ylo,zlo),(xhi,yhi,zhi)) covering every sampled position
        across all steps (computed by a physics pre-pass in run()). The camera
        looks at the box center and backs off far enough for the box to fit both
        the vertical and horizontal fov, plus margin.

        Default view is a straight SIDE view (camera on -Y, looking along +Y, at
        eye level) — clearest for reading vertical motion / collapse. Set
        ``camera_view: "three_quarter"`` for the old angled view. fov and margin
        are overridable; the box itself is data-driven.
        """
        import math
        fov = float(spec.get("camera_fov", 45.0))
        margin = float(spec.get("camera_margin", 0.7))
        view = str(spec.get("camera_view", "side")).lower()

        (xlo, ylo, zlo), (xhi, yhi, zhi) = aabb
        cx, cy, cz = (xlo + xhi) / 2, (ylo + yhi) / 2, (zlo + zhi) / 2
        ex, ey, ez = (xhi - xlo) / 2, (yhi - ylo) / 2, (zhi - zlo) / 2

        w, h = float(res[0]), float(res[1])
        vfov = math.radians(fov)
        hfov = 2.0 * math.atan(math.tan(vfov / 2.0) * (w / h))

        if view == "three_quarter":
            # Angled 3/4 view: camera backs off along +x.
            half_h = max(ez, 0.1) + 0.5 * ex
            half_w = max(ey, 0.1) + 0.5 * ex
            d_v = half_h / math.tan(vfov / 2.0)
            d_w = half_w / math.tan(hfov / 2.0)
            dist = max(d_v, d_w) * margin
            cam_pos = (cx + dist * 0.92, cy - dist * 0.18, cz + dist * 0.45)
            center = (cx, cy, cz)
        else:
            # Side-and-above view: camera on -Y, raised, looking down at the
            # scene. The frame is filled by the x-spread (horizontal) and
            # z-spread (vertical). Backing off a bit more + a real downward tilt
            # shows the whole scene (table surface, landing spot, how far the
            # material spreads), not just a thin eye-level slice.
            half_h = max(ez, 0.1)
            half_w = max(ex, 0.1)
            d_v = half_h / math.tan(vfov / 2.0)
            d_w = half_w / math.tan(hfov / 2.0)
            dist = max(d_v, d_w) * margin
            # Lift the camera well above center (fraction of distance) for a
            # clear high-side angle; aim at the lower part of the box (near the
            # ground/table) so the surface and spread are in frame.
            lift = float(spec.get("camera_height_frac", 0.85)) * dist
            aim_z = zlo + 0.25 * (zhi - zlo)
            cam_pos = (cx, cy - dist, cz + lift)
            center = (cx, cy, aim_z)
        return cam_pos, center, fov

    def _build_scene(self, gs, spec: Dict[str, Any]):
        dt = float(spec.get("dt", 0.01))
        gravity = tuple(spec.get("gravity", [0.0, 0.0, -9.81]))
        mats = {o.get("material", "rigid") for o in spec["objects"]}
        solvers = {_MATERIAL_SOLVER[m] for m in mats}

        # Sub-stepping is what keeps the particle solvers stable. With the
        # default substeps=1, MPM materials (elastic/snow especially) explode
        # and get pinned to the grid ceiling regardless of dt. Every working
        # Genesis MPM example uses 10-20 substeps; 20 holds all of
        # elastic/sand/snow at their default stiffness. SPH is also steadier
        # with a few substeps. Rigid/PBD-only scenes keep substeps=1 (fast).
        substeps = int(spec.get("substeps", 0)) or (
            20 if "mpm" in solvers else 10 if "sph" in solvers else 1
        )

        kwargs: Dict[str, Any] = dict(
            sim_options=gs.options.SimOptions(dt=dt, substeps=substeps, gravity=gravity),
            # shadow + plane_reflection both trigger long GPU render stalls on the
            # headless EGL stack here and are not needed for a motion-reference
            # clip (the downstream video model regenerates lighting). Kept off.
            vis_options=gs.options.VisOptions(shadow=False, plane_reflection=False),
            show_viewer=False,
        )
        if solvers & {"sph", "mpm"}:
            lo, hi = self._bounds(spec)
            if "sph" in solvers:
                kwargs["sph_options"] = gs.options.SPHOptions(lower_bound=lo, upper_bound=hi)
            if "mpm" in solvers:
                # grid_density controls how densely the MPM volume is sampled with
                # particles. We use Genesis' default of 64 (particle_size ~0.01):
                # a coarser 32 is faster but samples 8x fewer particles, so complex
                # meshes (dragon/bunny) lose their shape and read as a sparse blob.
                grid_density = float(spec.get("mpm_grid_density", 64))
                kwargs["mpm_options"] = gs.options.MPMOptions(
                    lower_bound=lo, upper_bound=hi, grid_density=grid_density)
        return gs.Scene(**kwargs)

    # -- main entry ----------------------------------------------------------
    def run(self, spec: Dict[str, Any]) -> Dict[str, Any]:
        gs = self._ensure_gs()
        # The ground plane is implicit: the scene ALWAYS has a floor at z=0, added
        # here so the planner never has to (and cannot) specify it. This keeps the
        # background consistent and stops objects falling through when the planner
        # forgets a floor. Skip if the spec somehow already carries a plane.
        objs = spec.setdefault("objects", [])
        if not any(o.get("type") == "plane" for o in objs):
            objs.append({"id": "floor", "type": "plane", "material": "rigid", "fixed": True})
        # Close tiny accidental gaps so bodies meant to rest on a surface start
        # touching it (the planner often miscomputes standing height by ~1-2 cm).
        # Airborne bodies (large gap / downward velocity / held) are left alone.
        self._snap_to_support(spec)
        dt = float(spec.get("dt", 0.01))
        # Particle solvers (SPH/MPM) need a small outer timestep; clamp to 0.008
        # so the planner never has to know solver internals. With the substeps set
        # in _build_scene (20 MPM / 10 SPH) this is ~2.7x fewer steps than the old
        # 0.003 clamp and is stable (sand/snow/elastic settle correctly, water
        # pools). NOTE: MPM may still log "substep_dt > suggested_dt" -- that is
        # governed by grid_density, NOT this dt, and is benign for the gentle
        # scenes here; to silence it lower MPMOptions grid_density (e.g. 32).
        mats = {o.get("material", "rigid") for o in spec["objects"]}
        if {_MATERIAL_SOLVER[m] for m in mats} & {"sph", "mpm"}:
            dt = min(dt, 0.008)
        # Timing is FIXED, not planner-controllable (spec duration/render_fps are
        # ignored). SLOW MOTION: simulate only SIM_PHYS_DURATION_S of real physics
        # (the active motion), then play those frames back over SIM_DURATION_S at
        # SIM_RENDER_FPS -> a (SIM_DURATION_S/SIM_PHYS_DURATION_S)x slow clip with
        # no static tail. seedance still receives a valid 1.8-15.2s reference.
        n_steps = max(1, int(round(SIM_PHYS_DURATION_S / dt)))
        # Rendering is always on: the reference video is the whole point of this
        # tool, so every simulation (any material/solver) produces a clip.
        render = True
        render_steps = set()
        render_fps: Optional[float] = None
        n_rendered_frames = 0
        if render:
            # sample (playback_seconds * fps) frames spread across the physics,
            # but tag the clip with the PLAYBACK fps so it stretches to 3 s.
            render_step_list, render_fps = self._render_schedule(
                n_steps, SIM_DURATION_S, SIM_RENDER_FPS)
            render_steps = set(render_step_list)

        # When rendering, do a cheap physics-only pre-pass first to learn the
        # exact box the objects sweep through, then frame the camera to THAT box
        # so nothing leaves the frame in later steps (a fixed play volume cannot
        # contain e.g. a 3 m parabola). The pre-pass skips rendering, so it is
        # just the (fast) physics stepping; the real pass renders with the camera.
        cam_aabb = None
        if render:
            pre = self._simulate(gs, spec, n_steps, render_steps=set(),
                                 cam_aabb=None, do_render=False)
            cam_aabb = self._traj_aabb(pre["traj"], spec)

        result = self._simulate(gs, spec, n_steps, render_steps=render_steps,
                                cam_aabb=cam_aabb, do_render=render)
        traj = result["traj"]
        video_path = None
        if render and result.get("cam") is not None:
            video_path = os.path.join(self.output_dir, f"sim_{abs(hash(str(spec))) % (10**8)}.mp4")
            out_fps = max(1, int(round(render_fps or 1.0 / dt)))
            result["cam"].stop_recording(save_to_filename=video_path, fps=out_fps)
            n_rendered_frames = result["n_rendered_frames"]
            # Drop the first recorded frame. Genesis' rasterizer shows a particle
            # body's pristine spawn lattice (SPH/MPM seed grid) on the very first
            # recorded frame even though the physics is already settled (the
            # visual particle mesh lags the solver state by one frame). The result
            # is a clip that opens on a perfect grid that then "snaps" to the real
            # shape on frame 2. We re-mux the file without that first frame,
            # duplicating the final frame to keep the frame count/duration.
            self._drop_first_frame(video_path, out_fps)

        summary = self._summarize(traj)
        out: Dict[str, Any] = {"trajectory": traj, "summary": summary,
                               "dt": dt, "n_steps": n_steps}
        if video_path:
            out["video_path"] = video_path
            out["render_fps"] = render_fps
            out["n_rendered_frames"] = n_rendered_frames
        return out

    def _simulate(self, gs, spec, n_steps, render_steps, cam_aabb, do_render):
        """Build the scene, apply initial velocities, step the physics, and
        (if do_render) record frames with a camera framed to cam_aabb.
        Returns {"traj", "cam", "n_rendered_frames"}."""
        scene = self._build_scene(gs, spec)

        entities: List[Tuple[str, Dict[str, Any], Any, bool]] = []
        for i, o in enumerate(spec["objects"]):
            morph = self._make_morph(gs, o)
            material = self._make_material(gs, o)
            surface = self._make_surface(gs, o)
            if surface is not None:
                ent = scene.add_entity(morph=morph, material=material, surface=surface)
            else:
                ent = scene.add_entity(morph=morph, material=material)
            oid = o.get("id", f"{o['type']}_{i}")
            is_particle = o.get("material", "rigid") in _PARTICLE_MATERIALS
            entities.append((oid, o, ent, is_particle))

        cam = None
        if do_render:
            res = tuple(spec.get("render_res", (1280, 720)))
            cam_pos, cam_lookat, cam_fov = self._frame_camera(spec, res, cam_aabb)
            cam = scene.add_camera(res=res, pos=cam_pos, lookat=cam_lookat,
                                   fov=cam_fov, GUI=False)

        scene.build()

        # initial velocities.
        #  - rigid free bodies: 6 dofs (3 lin + 3 ang) via set_dofs_velocity.
        #  - particle bodies (SPH/MPM/PBD): one uniform linear velocity applied to
        #    EVERY particle via set_velocity(), so a blob of water/sand/jelly/cloth
        #    can be thrown as a whole (parabola, sideways slide), not only dropped.
        #    Particles have no rigid angular dof, so init_angular is ignored for them.
        for oid, o, ent, is_particle in entities:
            if o.get("type") == "plane" or o.get("fixed", False):
                continue
            v = o.get("init_velocity")
            if v is None:
                continue
            if is_particle:
                ent.set_velocity(np.tile(np.asarray(v, dtype=float).reshape(1, 3),
                                         (ent.n_particles, 1)))
            else:
                w = o.get("init_angular", [0.0, 0.0, 0.0])
                try:
                    ent.set_dofs_velocity(list(v) + list(w))
                except Exception:
                    pass  # non-free / fixed-dof entities have no 6-dof velocity

        # Objects flagged `release_after_settle` are held frozen during the
        # settle phase (below) and let go when recording starts -- e.g. a duck
        # held in the air while the water beneath it comes to rest, then dropped
        # into the now-static pool. Capture their rest state to re-impose each
        # settle step.
        held = []
        for oid, o, ent, is_particle in entities:
            if not o.get("release_after_settle"):
                continue
            if is_particle:
                p0 = np.asarray(ent.get_particles_pos().cpu()
                                if hasattr(ent.get_particles_pos(), "cpu") else ent.get_particles_pos()).copy()
                held.append((ent, True, p0))
            else:
                pos0 = np.asarray(ent.get_pos().cpu() if hasattr(ent.get_pos(), "cpu") else ent.get_pos()).reshape(-1)[:3]
                held.append((ent, False, pos0))

        def _freeze_held():
            for ent, is_p, st in held:
                if is_p:
                    ent.set_velocity(np.zeros((ent.n_particles, 3), dtype=np.float32))
                    ent.set_position(_GS.tensor(st))
                else:
                    try:
                        ent.set_dofs_velocity([0.0] * 6)
                    except Exception:
                        pass

        # settle_first: run physics (no recording) until liquids come to rest, so
        # the recorded clip starts from a static surface instead of a settling
        # blob. Held objects stay frozen throughout.
        #
        # SPH liquid starts as a regular point lattice that, on the first ~50
        # steps, bursts upward under internal pressure (particles can shoot to 2x
        # the fill height) before falling back and settling around step ~120-150.
        # During that burst the mean speed dips THROUGH any loose threshold for a
        # frame or two (a false "at rest"), so a single-frame speed test bails out
        # mid-burst and recording starts on a half-collapsed surface. We therefore
        # (a) use a tight threshold and (b) require it to hold for CONSECUTIVE
        # frames, and (c) ignore the first MIN_SETTLE steps entirely so the initial
        # burst can never satisfy the test. Default max is generous; the loop exits
        # early once genuinely calm.
        if spec.get("settle_first"):
            liquids = [ent for _, o, ent, isp in entities
                       if isp and o.get("material") in ("liquid", "sand", "snow")]
            max_settle = int(spec.get("settle_max_steps", 400))
            calm_thresh = float(spec.get("settle_speed_thresh", 0.005))
            calm_needed = int(spec.get("settle_calm_frames", 10))
            min_settle = int(spec.get("settle_min_steps", 120))
            calm = 0
            for i in range(max_settle):
                scene.step()
                _freeze_held()
                if liquids:
                    spd = 0.0
                    for ent in liquids:
                        v = ent.get_particles_vel()
                        v = np.asarray(v.cpu() if hasattr(v, "cpu") else v)
                        spd = max(spd, float(np.linalg.norm(v, axis=1).mean()))
                    calm = calm + 1 if spd < calm_thresh else 0
                    if i >= min_settle and calm >= calm_needed:
                        break

        if do_render and cam is not None:
            cam.start_recording()

        # sample each non-fixed, non-plane object every step
        sampled = [(oid, ent, is_particle) for oid, o, ent, is_particle in entities
                   if o.get("type") != "plane" and not o.get("fixed", False)]
        traj: Dict[str, List[Any]] = {oid: [] for oid, _, _ in sampled}

        n_rendered_frames = 0
        for step_idx in range(n_steps):
            scene.step()
            for oid, ent, is_particle in sampled:
                if is_particle:
                    p = ent.get_particles_pos()
                    p = np.asarray(p.cpu() if hasattr(p, "cpu") else p)
                    traj[oid].append(p.mean(axis=0).tolist())  # centroid per step
                else:
                    p = ent.get_pos()
                    p = np.asarray(p.cpu() if hasattr(p, "cpu") else p)
                    traj[oid].append(p.reshape(-1)[:3].tolist())
            if do_render and cam is not None and step_idx in render_steps:
                cam.render()
                n_rendered_frames += 1

        return {"traj": traj, "cam": cam, "n_rendered_frames": n_rendered_frames}

    @staticmethod
    def _traj_aabb(traj: Dict[str, List[Any]], spec: Dict[str, Any]) -> Tuple[tuple, tuple]:
        """Axis-aligned box covering every sampled position over the whole clip,
        padded by the largest object's half-size and a small margin, with the
        floor (z=0) always included so the ground is in frame."""
        pts = [p for pts in traj.values() for p in pts]
        if not pts:
            return (-1.0, -1.0, 0.0), (1.0, 1.0, 1.0)
        arr = np.asarray(pts, dtype=float)
        lo = arr.min(axis=0)
        hi = arr.max(axis=0)
        # pad by the biggest object extent so bodies aren't clipped at the edge
        max_half = 0.1
        for o in spec["objects"]:
            if o.get("type") == "plane":
                continue
            max_half = max(max_half, float(np.max(SimCore._obj_half_extent(o))))
        pad = max_half + 0.15
        lo = lo - pad
        hi = hi + pad
        lo[2] = min(lo[2], 0.0)  # keep the floor in view
        return tuple(lo.tolist()), tuple(hi.tolist())

    @staticmethod
    def _render_schedule(n_steps: int, duration: float, requested_fps: float) -> Tuple[List[int], float]:
        if n_steps <= 0:
            raise ValueError("n_steps must be positive")
        if duration <= 0:
            raise ValueError("duration must be positive")
        target_fps = requested_fps if requested_fps > 0 else 24.0
        # Render ONE extra frame: the first recorded frame is dropped afterwards
        # (Genesis shows a particle body's spawn lattice on frame 0 — see
        # _drop_first_frame), so we capture n_frames+1 to still deliver n_frames.
        n_frames = max(1, int(round(duration * target_fps)))
        n_capture = n_frames + 1
        if n_capture > n_steps:
            raise ValueError(
                f"render_fps={target_fps:g} needs {n_capture} frames for {duration:g}s, "
                f"but the simulation only has {n_steps} steps; decrease dt or render_fps"
            )
        steps = np.linspace(0, n_steps - 1, n_capture, dtype=int).tolist()
        return steps, target_fps

    @staticmethod
    def _drop_first_frame(video_path: str, fps: int) -> None:
        """Rewrite video_path without its first frame, via imageio.

        Genesis records the spawn-lattice frame as frame 0 (the visual particle
        mesh lags the solver by one frame), so a clip opens on a perfect grid that
        snaps to the real shape on frame 2. We read every frame, drop index 0, and
        re-encode the rest at the same fps. Uses imageio (imageio-ffmpeg backend,
        a bundled static ffmpeg) instead of shelling out to a system ffmpeg, so
        there is no dependency on a CLI binary on PATH.
        """
        import imageio.v3 as iio  # noqa: PLC0415

        frames = iio.imread(video_path, plugin="FFMPEG")  # (n, h, w, 3)
        if len(frames) <= 1:
            return
        tmp = video_path + ".tmp.mp4"
        iio.imwrite(tmp, frames[1:], plugin="FFMPEG", fps=fps, codec="libx264")
        os.replace(tmp, video_path)

    @staticmethod
    def _summarize(traj: Dict[str, List[Any]]) -> str:
        parts = []
        for oid, pts in traj.items():
            if not pts:
                continue
            start = np.array(pts[0]); end = np.array(pts[-1])
            zs = [p[2] for p in pts]
            disp = float(np.linalg.norm(end - start))
            parts.append(f"{oid}: moved {disp:.2f} m, z {start[2]:.2f}->{end[2]:.2f} "
                         f"(min {min(zs):.2f}, max {max(zs):.2f})")
        return "; ".join(parts) if parts else "no movable objects sampled"

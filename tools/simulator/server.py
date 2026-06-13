"""Simulator MCP server for OpenNewton.

FastMCP server exposing the Genesis physics simulator the planner can call via
native function calling. The planner never writes JSON by hand: it
selects ``simulate`` and fills a structured ``scene_spec``; the executor builds
the Genesis scene, runs the physics, and returns the resulting motion.

Tool:
  - ``simulate`` : run a physics simulation of a ``scene_spec`` (objects +
                   materials + initial state + gravity). Returns per-object
                   trajectories and a summary, and always renders a
                   physics-correct reference video (fixed 3.0s @ 24fps).

The planner-facing usage guidance lives in
``planner_skills/simulator.md``. material -> solver routing is handled in core.

Run as an MCP stdio server (default) or ``--test`` for a smoke check.
"""

from __future__ import annotations

import multiprocessing as mp
import os
import queue
import sys
from typing import Any, Dict, List, Optional

from mcp.server.fastmcp import FastMCP

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from core import SimCore  # noqa: E402


# Native function-calling schema advertised to the planner.
tool_configs: List[Dict[str, object]] = [
    {
        "type": "function",
        "function": {
            "name": "simulate",
            "description": (
                "Run a physics simulation of a scene and get a physics-correct "
                "REFERENCE VIDEO of the motion (plus per-object trajectories). Call "
                "this whenever the motion is hard for a video generator to get "
                "right on its own: unusual-material or counter-intuitive behavior "
                "(a duck made of sand collapsing, a clay figure denting, a jelly "
                "cube wobbling, cloth draping), fluid / granular / soft-body flow "
                "(pouring liquid, sand collapse), or contact-rich / multi-body "
                "dynamics (collisions, stacking, toppling). Always renders the "
                "reference video."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "scene_spec": {
                        "type": "object",
                        "description": (
                            "Structured scene description. Decompose every object "
                            "into a primitive shape with real dimensions in meters."
                        ),
                        "properties": {
                            "objects": {
                                "type": "array",
                                "description": "All bodies in the scene.",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "id": {"type": "string", "description": "Label, used as the trajectory key."},
                                        "type": {
                                            "type": "string",
                                            "enum": ["sphere", "box", "cylinder", "mesh"],
                                            "description": "Primitive shape, or 'mesh' for a built-in real object (set `asset`).",
                                        },
                                        "asset": {
                                            "type": "string",
                                            "enum": ["duck", "bunny", "dragon", "sphere"],
                                            "description": "For type='mesh': which built-in Genesis mesh to use (no file import).",
                                        },
                                        "scale": {"type": "number", "description": "RIGID mesh only: uniform scale. IGNORED for soft/granular (MPM) meshes — those are auto-sized to a fixed, clearly-visible size, so do not set scale for a clay/jelly/etc. mesh."},
                                        "material": {
                                            "type": "string",
                                            "enum": ["rigid", "liquid", "sand", "snow", "elastic", "elastoplastic", "cloth"],
                                            "description": "Physical material; selects the solver. Use 'elastic' for springy solids that rebound (jelly, rubber); 'elastoplastic' for putty/plasticine/dough/clay that DENTS PERMANENTLY and does not spring back.",
                                        },
                                        "pos": {
                                            "type": "array", "items": {"type": "number"},
                                            "description": "Initial center [x,y,z] in meters, z up.",
                                        },
                                        "radius": {"type": "number", "description": "Sphere/cylinder radius (m)."},
                                        "size": {
                                            "type": "array", "items": {"type": "number"},
                                            "description": "Box size [x,y,z] (m); for a fluid/granular body, its initial volume.",
                                        },
                                        "density": {"type": "number", "description": "kg/m^3 (water~1000, wood~600, steel~7800)."},
                                        "stiffness": {"type": "number", "description": "elastic/elastoplastic only: Young's modulus E (default 3e5). Lower = softer/wobblier (e.g. 3e4 very soft, 3e5 firm)."},
                                        "yield_stress": {"type": "number", "description": "elastoplastic only: von Mises yield stress (default 6e3, a firm clay that clearly dents). LOWER = dents/deforms more easily; too low (<1e3) makes it collapse into a puddle; higher (>1e4) barely deforms."},
                                        "init_velocity": {
                                            "type": "array", "items": {"type": "number"},
                                            "description": "Initial linear velocity [vx,vy,vz] m/s at t=0.",
                                        },
                                        "init_angular": {
                                            "type": "array", "items": {"type": "number"},
                                            "description": "Initial angular velocity [wx,wy,wz] rad/s at t=0.",
                                        },
                                        "euler": {
                                            "type": "array", "items": {"type": "number"},
                                            "description": "Optional orientation [rx,ry,rz] in degrees (e.g. tilt a ramp).",
                                        },
                                        "fixed": {"type": "boolean", "description": "true = immovable (table, container, floor)."},
                                        "color": {
                                            "type": "array", "items": {"type": "number"},
                                            "description": "Optional render color [r,g,b] in 0-1 (works for any material, including rigid).",
                                        },
                                        "transparent": {
                                            "type": "boolean",
                                            "description": "Rigid only: render see-through (alpha) so contents stay visible — use for a glass-like container/tank.",
                                        },
                                        "opacity": {
                                            "type": "number",
                                            "description": "Rigid only: 0-1 alpha when transparent (default 0.3; lower = clearer).",
                                        },
                                        "release_after_settle": {
                                            "type": "boolean",
                                            "description": "Hold this body frozen in place during the settle phase (see scene_spec.settle_first), then release it when recording starts — e.g. an object held in the air that drops into a now-static pool. Requires settle_first.",
                                        },
                                    },
                                    "required": ["type", "material"],
                                },
                            },
                            "gravity": {
                                "type": "array", "items": {"type": "number"},
                                "description": "Gravity vector m/s^2, default [0,0,-9.81].",
                            },
                            "settle_first": {
                                "type": "boolean",
                                "description": "Run physics until liquids come to rest BEFORE recording, so the clip starts from a calm, static surface (e.g. a still pool) instead of a settling blob. Pair with a body's release_after_settle to drop something into the settled liquid.",
                            },
                        },
                        "required": ["objects"],
                    },
                },
                "required": ["scene_spec"],
            },
        },
    },
]


mcp = FastMCP("simulator-executor")

_core: Optional[SimCore] = None


@mcp.tool()
def initialize(args: Dict[str, object]) -> Dict[str, object]:
    """Initialize the simulator core from config/env and return tool_configs.

    Recognized keys in ``args`` (all optional): ``backend`` ('gpu'|'cpu'),
    ``output_dir``.
    """
    global _core
    try:
        a: Dict[str, Any] = dict(args or {})
        _core = SimCore(
            backend=a.get("backend", os.environ.get("SIMULATOR_BACKEND", "gpu")),
            output_dir=a.get("output_dir"),
        )
        return {
            "status": "success",
            "output": {"text": ["Simulator executor initialized"], "tool_configs": tool_configs},
        }
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "output": {"text": [str(e)]}}


def _ensure_core() -> SimCore:
    global _core
    if _core is None:
        _core = SimCore(backend=os.environ.get("SIMULATOR_BACKEND", "gpu"))
    return _core


def _run_process_with_timeout(target, timeout: float, args: tuple = ()) -> Dict[str, Any]:
    ctx = mp.get_context("spawn")
    q = ctx.Queue()
    proc = ctx.Process(target=target, args=(q, *args))
    proc.start()
    proc.join(timeout)
    if proc.is_alive():
        proc.terminate()
        proc.join(5)
        if proc.is_alive():
            proc.kill()
            proc.join()
        q.close()
        return {"status": "timeout", "error": f"worker exceeded {timeout:.1f}s"}
    try:
        return q.get_nowait()
    except queue.Empty:
        return {"status": "error", "error": f"worker exited with code {proc.exitcode} without a result"}
    finally:
        q.close()


def _render_worker(queue, scene_spec: Dict[str, Any], backend: str, output_dir: str) -> None:
    try:
        result = SimCore(backend=backend, output_dir=output_dir).run(scene_spec)
        queue.put({
            "status": "success",
            "output": {
                k: result[k]
                for k in ("video_path", "render_fps", "n_rendered_frames")
                if k in result
            },
        })
    except Exception as e:  # noqa: BLE001
        queue.put({"status": "error", "error": str(e)})


def _run_simulation_with_isolated_render(core: SimCore, scene_spec: Dict[str, Any]) -> Dict[str, Any]:
    no_render_spec = dict(scene_spec)
    no_render_spec["render"] = False
    result = core.run(no_render_spec)

    render_timeout = float(
        scene_spec.get("render_timeout", os.environ.get("SIMULATOR_RENDER_TIMEOUT", 180.0))
    )
    render_result = _run_process_with_timeout(
        _render_worker,
        timeout=render_timeout,
        args=(dict(scene_spec), core.backend, core.output_dir),
    )
    if render_result.get("status") == "success":
        result.update(render_result.get("output", {}))
    else:
        result["render_error"] = render_result.get("error", render_result.get("status", "unknown render error"))
    return result


@mcp.tool()
def simulate(scene_spec: Dict[str, Any]) -> Dict[str, object]:
    """Run a Genesis physics simulation of scene_spec; return motion + summary."""
    try:
        core = _ensure_core()
        # Rendering is always on (core forces it). It runs in the resident process
        # by default: it reuses the initialized Genesis context, so only the first
        # request pays the ~13s startup and later renders are fast. Set
        # SIMULATOR_RENDER_ISOLATED=1 to fall back to a fresh per-render subprocess.
        isolate_render = os.environ.get("SIMULATOR_RENDER_ISOLATED", "0") != "0"
        if isolate_render:
            result = _run_simulation_with_isolated_render(core, scene_spec)
        else:
            result = core.run(scene_spec)
        text = [result["summary"]]
        if result.get("video_path"):
            text.append(f"reference video: {result['video_path']}")
        if result.get("render_error"):
            text.append(f"reference video unavailable: {result['render_error']}")
        return {"status": "success", "output": {"text": text, "records": result}}
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "output": {"text": [str(e)]}}


def main() -> None:
    """Run the MCP server, or ``--test`` for a local smoke check."""
    if len(sys.argv) > 1 and sys.argv[1] == "--test":
        print("initialize:", initialize({}))
        spec = {
            "objects": [
                {"id": "ball", "type": "sphere", "material": "rigid",
                 "pos": [0, 0, 1.0], "radius": 0.05, "density": 7800},
                {"id": "floor", "type": "plane", "material": "rigid", "fixed": True},
            ],
            "duration": 1.0, "dt": 0.01,
        }
        print("simulate:", simulate(spec)["output"]["text"])
    else:
        mcp.run()


if __name__ == "__main__":
    main()

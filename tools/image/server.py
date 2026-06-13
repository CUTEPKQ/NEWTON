"""Image MCP server for OpenNewton.

FastMCP server exposing the image tools the planner can call via native function
calling. The planner never writes JSON by hand: it selects a
tool and fills arguments; the schema below is what the executor advertises.

Tools:
  - ``img_create``     : produce ONE image (text-to-image, or edit when
                         ``reference_images`` is given). Returns its local path.
  - ``make_keyframes`` : produce a FIRST + LAST key-frame pair. The last frame
                         is generated as an edit of the first so the two share
                         subject/scene/lighting and differ only in physical
                         state. Returns both local paths in order.

Each tool returns ``{status, output: {text: [...], records: [...]}}`` where
``records`` carry ``{prompt, local_path, mode, index}``.

Run as an MCP stdio server (default) or ``--test`` for a smoke check.
"""

from __future__ import annotations

import os
import sys
from typing import Any, Dict, List, Optional

from mcp.server.fastmcp import FastMCP

# Self-contained core — no dependency on the legacy .claude/skills tree.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from core import ImageCore  # noqa: E402


# Native function-calling schema advertised to the planner.
tool_configs: List[Dict[str, object]] = [
    {
        "type": "function",
        "function": {
            "name": "img_create",
            "description": (
                "Create ONE image and return its local path. With no reference "
                "image it generates from text (text-to-image); with one or more "
                "reference images it edits / re-conditions on them "
                "(image-to-image). Use for a single key frame or a revision of "
                "an existing image. For a first+last key-frame pair, use "
                "make_keyframes instead."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": (
                            "One concrete description of the single image. For an "
                            "edit, describe the RESULTING visible state (use "
                            "'Change/Replace/<subject> is now ...'), not a "
                            "real-world action verb."
                        ),
                    },
                    "reference_images": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Local image paths to condition on. Omit or leave "
                            "empty for text-to-image; non-empty switches to edit."
                        ),
                    },
                    "as_first_frame": {
                        "type": "boolean",
                        "description": (
                            "If true, stage this image as the video's i2v FIRST "
                            "FRAME (a STRONG constraint: the generated video starts "
                            "on this exact image) instead of a weak reference "
                            "image. Use when the image already shows the correct "
                            "scene (e.g. the exact object count/layout) and you "
                            "want the generator anchored to it."
                        ),
                    },
                },
                "required": ["prompt"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "make_keyframes",
            "description": (
                "Create a FIRST and a LAST key frame for a video. The first "
                "frame is the pre-action still; the last frame is generated as "
                "an edit of the first, so both share subject, scene and lighting "
                "and differ only in physical state. Returns both local paths in "
                "order (first, last) for the video generator."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "first_prompt": {
                        "type": "string",
                        "description": (
                            "Text-to-image caption of the full PRE-ACTION scene: "
                            "subject, location, key objects, viewpoint, lighting. "
                            "Initial state only, no motion blur."
                        ),
                    },
                    "last_prompt": {
                        "type": "string",
                        "description": (
                            "Edit instruction describing the FINAL visible state "
                            "after the whole action, as an edit of the first frame "
                            "('Change/Replace/<subject> is now ...'). Include every "
                            "element that physically responds (pose, contact "
                            "effects, shadows, deformation). Must be visually "
                            "distinct from the first frame."
                        ),
                    },
                    "reference_images": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Optional local image paths to seed the FIRST frame "
                            "(e.g. a reference photo). Empty → first frame is "
                            "text-to-image."
                        ),
                    },
                },
                "required": ["first_prompt", "last_prompt"],
            },
        },
    },
]


mcp = FastMCP("image-executor")

_core: Optional[ImageCore] = None


@mcp.tool()
def initialize(args: Dict[str, object]) -> Dict[str, object]:
    """Initialize the image core from config/env and return tool_configs.

    Recognized keys in ``args`` (all optional; fall back to env vars):
    ``api_key``, ``base_url``, ``model``, ``output_dir``, ``resolution``,
    ``supported_sizes``, ``auth_header``.
    """
    global _core
    try:
        a: Dict[str, Any] = dict(args or {})
        _core = ImageCore(
            api_key=a.get("api_key"),
            base_url=a.get("base_url"),
            model=a.get("model"),
            output_dir=a.get("output_dir"),
            resolution=a.get("resolution"),
            supported_sizes=a.get("supported_sizes"),
            auth_header=a.get("auth_header"),
        )
        return {
            "status": "success",
            "output": {"text": ["Image executor initialized"], "tool_configs": tool_configs},
        }
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "output": {"text": [str(e)]}}


def _ensure_core() -> ImageCore:
    global _core
    if _core is None:
        # Lazily build from env so the tool also works without an explicit init.
        _core = ImageCore()
    return _core


@mcp.tool()
def img_create(prompt: str, reference_images: Optional[List[str]] = None,
               as_first_frame: bool = False) -> Dict[str, object]:
    """Create one image (text-to-image, or edit when reference_images given).

    ``as_first_frame`` is a staging hint for the orchestrator (whether to use the
    image as the i2v first frame vs a reference image); it does not change image
    generation here.
    """
    try:
        core = _ensure_core()
        records = core.generate_or_edit(prompt=prompt, image_paths=reference_images or [])
        paths = [r["local_path"] for r in records]
        return {
            "status": "success",
            "output": {
                "text": [f"Created {len(paths)} image(s): " + ", ".join(paths)],
                "records": records,
            },
        }
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "output": {"text": [str(e)]}}


@mcp.tool()
def make_keyframes(
    first_prompt: str,
    last_prompt: str,
    reference_images: Optional[List[str]] = None,
) -> Dict[str, object]:
    """Create a first + last key-frame pair (last edits the first)."""
    try:
        core = _ensure_core()
        records = core.keyframes(
            first_prompt=first_prompt,
            last_prompt=last_prompt,
            reference_images=reference_images or [],
        )
        paths = [r["local_path"] for r in records]
        return {
            "status": "success",
            "output": {
                "text": [
                    "Key frames ready — first: "
                    f"{paths[0] if paths else '?'}, last: {paths[1] if len(paths) > 1 else '?'}"
                ],
                "records": records,
            },
        }
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "output": {"text": [str(e)]}}


def main() -> None:
    """Run the MCP server, or ``--test`` for a local smoke check."""
    if len(sys.argv) > 1 and sys.argv[1] == "--test":
        print("initialize:", initialize({}))
        print("img_create:", img_create("a red cube on a white table, soft studio light"))
        print(
            "make_keyframes:",
            make_keyframes(
                "A full glass of water upright on a wooden table, side view, soft daylight.",
                "The glass is now on its side, water spilled into a spreading puddle, same table and light.",
            ),
        )
    else:
        mcp.run()


if __name__ == "__main__":
    main()

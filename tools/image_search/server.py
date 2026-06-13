"""Image-search MCP server for OpenNewton.

FastMCP server exposing a text-to-image web search the planner can call via
native function calling. The planner never writes JSON by hand: it
selects ``image_search`` and fills arguments; the executor queries the search
backend, downloads each result image to a local cache, and returns the local
paths so downstream steps (img_create as a reference, or the video generator)
can consume real photos.

Tool:
  - ``image_search`` : text-to-image web search. Returns image results with
                       titles, source page urls and LOCAL downloaded paths.

The planner-facing usage guidance lives in
``planner_skills/Image_search.md``. Backend protocol (Serper /images) is handled
in core.

Run as an MCP stdio server (default) or ``--test`` for a smoke check.
"""

from __future__ import annotations

import os
import sys
from typing import Any, Dict, List, Optional

from mcp.server.fastmcp import FastMCP

# Self-contained core — no dependency on the legacy .claude/skills tree.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from core import ImageSearchCore  # noqa: E402


# Native function-calling schema advertised to the planner.
tool_configs: List[Dict[str, object]] = [
    {
        "type": "function",
        "function": {
            "name": "image_search",
            "description": (
                "Text-to-image web search. Returns up to top_k real "
                "photographs/illustrations matching a descriptive query, each "
                "downloaded to a LOCAL path AND previewed as a small inline "
                "thumbnail image so you can SEE the candidates and choose the best "
                "one (then use its local_path downstream). Use to fetch a real "
                "reference image (a specific object, character, person, place or "
                "style) to condition img_create or the video generator. To "
                "synthesize a new image from text use img_create instead."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "Descriptive text query for the image you need — be "
                            "concrete about subject, attributes and viewpoint "
                            "(e.g. 'red ceramic coffee mug on white background, "
                            "studio photo')."
                        ),
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "Max images to return (default 3). Keep small — more candidates cost more tokens and search quota.",
                    },
                },
                "required": ["query"],
            },
        },
    },
]


mcp = FastMCP("image-search-executor")

_core: Optional[ImageSearchCore] = None


@mcp.tool()
def initialize(args: Dict[str, object]) -> Dict[str, object]:
    """Initialize the image-search core from config/env and return tool_configs.

    Recognized keys in ``args`` (all optional; fall back to env vars):
    ``api_key`` (SERPER_API_KEY), ``base_url`` (SERPER_BASE_URL),
    ``download_dir`` (IMAGE_SEARCH_OUTPUT_DIR).
    """
    global _core
    try:
        a: Dict[str, Any] = dict(args or {})
        _core = ImageSearchCore(
            api_key=a.get("api_key"),
            base_url=a.get("base_url"),
            download_dir=a.get("download_dir"),
        )
        return {
            "status": "success",
            "output": {"text": ["Image-search executor initialized"], "tool_configs": tool_configs},
        }
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "output": {"text": [str(e)]}}


def _ensure_core() -> ImageSearchCore:
    global _core
    if _core is None:
        # Lazily build from env so the tool also works without an explicit init.
        _core = ImageSearchCore()
    return _core


@mcp.tool()
def image_search(query: str, top_k: int = 3) -> Dict[str, object]:
    """Search the web for images matching query; return local paths + thumbnails.

    Each record has ``index``, ``local_path`` (use this downstream), ``title``
    and a base64 ``thumbnail`` data URL. ``output.images`` lists the thumbnails in
    index order so the executor can inject them as image blocks for the planner to
    SEE and choose from; the planner then references the chosen record's
    ``local_path``.
    """
    try:
        core = _ensure_core()
        records = core.search(query=query, top_k=top_k)
        if records:
            text = [f"Found {len(records)} image(s) for '{query}' (pick one by index):"]
            for r in records:
                text.append(f"  [{r['index']}] {r['title']} -> {r['local_path']}")
        else:
            text = [f"No downloadable images found for '{query}'."]
        images = [
            {"index": r["index"], "local_path": r["local_path"], "thumbnail": r["thumbnail"]}
            for r in records
            if r.get("thumbnail")
        ]
        return {"status": "success", "output": {"text": text, "records": records, "images": images}}
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "output": {"text": [str(e)]}}


def main() -> None:
    """Run the MCP server, or ``--test`` for a local smoke check."""
    if len(sys.argv) > 1 and sys.argv[1] == "--test":
        print("initialize:", initialize({}))
        print("image_search:", image_search("a red ceramic coffee mug on a white table, studio photo", top_k=3))
    else:
        mcp.run()


if __name__ == "__main__":
    main()

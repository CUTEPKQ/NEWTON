#!/usr/bin/env python3
"""Function-call regression test for OpenNewton tool servers.

Validates that a planner LLM, given a tool server's `tool_configs`, picks the
right tool and fills its required arguments — independent of any image actually
being generated (no GPU, no IMG_CREATE_* needed).

Endpoint-agnostic: point it at Azure, OpenAI, a vLLM server, or any
OpenAI-compatible chat-completions endpoint. The planner backend is NOT fixed —
swap it via env / CLI.

Config (env vars; CLI flags override):
  PLANNER_API_KEY    api key for the chat endpoint            (required)
  PLANNER_BASE_URL   OpenAI-compatible base url               (required)
  PLANNER_MODEL      model / deployment name                  (required)
  PLANNER_AUTH       'api-key' (Azure, default) or 'bearer'

Examples:
  # Azure gpt-5.5 (api-key header; gpt-5.x needs max_completion_tokens — handled)
  PLANNER_API_KEY=... \\
  PLANNER_BASE_URL=https://<resource>.services.ai.azure.com/openai/v1/ \\
  PLANNER_MODEL=gpt-5.5 \\
  python open-newton/tools/test_function_call.py

  # Local vLLM (needs server started with --enable-auto-tool-choice
  #             --tool-call-parser hermes)
  PLANNER_API_KEY=EMPTY PLANNER_AUTH=bearer \\
  PLANNER_BASE_URL=http://localhost:8000/v1 PLANNER_MODEL=Qwen3.5-9B \\
  python open-newton/tools/test_function_call.py

Exit code is non-zero if any case fails, so it drops into CI / pre-commit.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List

try:
    from openai import OpenAI
except ImportError:  # pragma: no cover
    print("The `openai` package is required: pip install 'openai>=1.30'", file=sys.stderr)
    raise

THIS_DIR = Path(__file__).resolve().parent


# ---------------------------------------------------------------------------
# Load a tool server's tool_configs by file path (no package install needed).
# ---------------------------------------------------------------------------
def load_tool_configs(server_path: Path) -> List[Dict[str, Any]]:
    server_dir = str(server_path.parent)
    if server_dir not in sys.path:
        sys.path.insert(0, server_dir)
    spec = importlib.util.spec_from_file_location(server_path.stem, server_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load tool server: {server_path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    configs = getattr(mod, "tool_configs", None)
    if not configs:
        raise RuntimeError(f"{server_path} has no `tool_configs`")
    return configs


# ---------------------------------------------------------------------------
# Assertions a case can make about the model's tool call.
# A check returns "" on success or a failure reason.
# ---------------------------------------------------------------------------
def expect_tool(name: str) -> Callable[[str, Dict[str, Any]], str]:
    def check(tool_name: str, _args: Dict[str, Any]) -> str:
        return "" if tool_name == name else f"expected tool {name!r}, got {tool_name!r}"
    return check


def expect_nonempty(*fields: str) -> Callable[[str, Dict[str, Any]], str]:
    def check(_tool_name: str, args: Dict[str, Any]) -> str:
        missing = [f for f in fields if not str(args.get(f, "")).strip()]
        return "" if not missing else f"empty/missing fields: {missing}"
    return check


def expect_list_nonempty(field: str) -> Callable[[str, Dict[str, Any]], str]:
    def check(_tool_name: str, args: Dict[str, Any]) -> str:
        v = args.get(field)
        return "" if isinstance(v, list) and len(v) > 0 else f"{field!r} must be a non-empty list"
    return check


def expect_contains(field: str, substr: str) -> Callable[[str, Dict[str, Any]], str]:
    def check(_tool_name: str, args: Dict[str, Any]) -> str:
        v = json.dumps(args.get(field, ""))
        return "" if substr in v else f"{field!r} should contain {substr!r}"
    return check


# ---------------------------------------------------------------------------
# Test cases. Add a dict here to cover a new skill / tool / scenario.
# Each case: name, user prompt, and a list of checks (all must pass).
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = (
    "You are the Planner of a physics video generation system. To anchor a "
    "video you create key frames via the provided tools. Choose the right tool "
    "and fill its arguments. Use exactly one tool call."
)

CASES: List[Dict[str, Any]] = [
    {
        "name": "tip-over scenario -> make_keyframes (first+last)",
        "user": "Scenario: A glass of water tips over on a wooden table and spills.",
        "checks": [
            expect_tool("make_keyframes"),
            expect_nonempty("first_prompt", "last_prompt"),
        ],
    },
    {
        "name": "archer scenario -> make_keyframes",
        "user": "Scenario: An archer draws a recurve bow and releases the arrow into the target.",
        "checks": [
            expect_tool("make_keyframes"),
            expect_nonempty("first_prompt", "last_prompt"),
        ],
    },
    {
        "name": "single edit of existing image -> img_create with reference",
        "user": (
            "I already have the image outputs/images/apple.png. Make the apple "
            "redder and add a soft drop shadow. I only need that one edited image."
        ),
        "checks": [
            expect_tool("img_create"),
            expect_nonempty("prompt"),
            expect_list_nonempty("reference_images"),
            expect_contains("reference_images", "apple.png"),
        ],
    },
    {
        "name": "single text-to-image -> img_create no reference",
        "user": (
            "Generate one still image from scratch: a red cube on a white "
            "studio table under soft light. Just one image."
        ),
        "checks": [
            expect_tool("img_create"),
            expect_nonempty("prompt"),
        ],
    },
]


# ---------------------------------------------------------------------------
# Endpoint config + a single chat call that tolerates gpt-5.x token-param quirk.
# ---------------------------------------------------------------------------
def build_client(base_url: str, api_key: str, auth: str) -> OpenAI:
    headers = {"api-key": api_key} if auth == "api-key" else None
    return OpenAI(base_url=base_url, api_key=api_key, default_headers=headers)


def call_with_tools(
    client: OpenAI,
    model: str,
    messages: List[Dict[str, str]],
    tools: List[Dict[str, Any]],
    max_tokens: int = 2048,
):
    """Chat call with tools; retries with max_completion_tokens for gpt-5.x."""
    kwargs: Dict[str, Any] = dict(
        model=model, messages=messages, tools=tools, tool_choice="auto"
    )
    try:
        return client.chat.completions.create(max_tokens=max_tokens, **kwargs)
    except Exception as exc:  # noqa: BLE001
        if "max_completion_tokens" in str(exc) or "max_tokens" in str(exc):
            return client.chat.completions.create(max_completion_tokens=max_tokens, **kwargs)
        raise


# ---------------------------------------------------------------------------
def run(server_path: Path, base_url: str, api_key: str, model: str, auth: str, verbose: bool) -> int:
    tools = load_tool_configs(server_path)
    tool_names = [t["function"]["name"] for t in tools]
    print(f"server : {server_path}")
    print(f"tools  : {tool_names}")
    print(f"model  : {model}  @ {base_url}\n")

    client = build_client(base_url, api_key, auth)
    passed = 0
    for case in CASES:
        name = case["name"]
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": case["user"]},
        ]
        try:
            resp = call_with_tools(client, model, messages, tools)
            msg = resp.choices[0].message
        except Exception as exc:  # noqa: BLE001
            print(f"[ERROR] {name}\n        request failed: {exc}")
            continue

        if not msg.tool_calls:
            print(f"[FAIL ] {name}\n        no tool call (content: {(msg.content or '')[:120]!r})")
            continue

        tc = msg.tool_calls[0]
        tool_name = tc.function.name
        try:
            args = json.loads(tc.function.arguments)
        except Exception as exc:  # noqa: BLE001
            print(f"[FAIL ] {name}\n        invalid JSON arguments: {exc}")
            continue

        reasons = [r for r in (chk(tool_name, args) for chk in case["checks"]) if r]
        if reasons:
            print(f"[FAIL ] {name} -> {tool_name}")
            for r in reasons:
                print(f"        - {r}")
            if verbose:
                print("        args:", json.dumps(args, ensure_ascii=False))
            continue

        passed += 1
        print(f"[PASS ] {name} -> {tool_name}")
        if verbose:
            print("        args:", json.dumps(args, ensure_ascii=False, indent=2))

    total = len(CASES)
    print(f"\n{passed}/{total} cases passed")
    return 0 if passed == total else 1


def main() -> None:
    ap = argparse.ArgumentParser(description="Function-call regression test for OpenNewton tools.")
    ap.add_argument("--server", default=str(THIS_DIR / "image" / "server.py"),
                    help="Path to a tool server exposing `tool_configs` (default: image/server.py).")
    ap.add_argument("--base-url", default=os.environ.get("PLANNER_BASE_URL"))
    ap.add_argument("--api-key", default=os.environ.get("PLANNER_API_KEY"))
    ap.add_argument("--model", default=os.environ.get("PLANNER_MODEL"))
    ap.add_argument("--auth", default=os.environ.get("PLANNER_AUTH", "api-key"),
                    choices=["api-key", "bearer"])
    ap.add_argument("-v", "--verbose", action="store_true", help="Print tool-call arguments.")
    args = ap.parse_args()

    missing = [n for n, v in [("PLANNER_BASE_URL/--base-url", args.base_url),
                              ("PLANNER_API_KEY/--api-key", args.api_key),
                              ("PLANNER_MODEL/--model", args.model)] if not v]
    if missing:
        ap.error("missing required config: " + ", ".join(missing))

    sys.exit(run(Path(args.server).resolve(), args.base_url, args.api_key, args.model, args.auth, args.verbose))


if __name__ == "__main__":
    main()

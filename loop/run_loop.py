"""OpenNewton inference loop — gpt-5.5 planner + tools + seedance + Gemini verifier.

A minimal, training-free orchestrator that turns a physics scenario (a natural
language `question`) into a physics-grounded video. Each turn:

  1. The planner (gpt-5.5, native function-calling) may call preparation tools
     (image_search, img_create, make_keyframes, simulate) any number of times.
     Tool results are fed back into the conversation. This follows the GenEvolve
     pattern: keep calling tools until the model replies with plain text.
  2. That final plain-text reply IS the `video_prompt` (this also absorbs the
     no-tool `prompt_refine` skill — its output is just text).
  3. The loop then ALWAYS generates a video with seedance, conditioned on the
     prompt plus whatever reference images / reference video the tools produced
     (staged during step 1).
  4. The Gemini verifier scores the video (SA = semantic adherence to the
     question, PC = physical commonsense, 1-5) and self-judges STOP / CONTINUE.
  5. The verifier verdict is injected back into the conversation so the planner
     can refine on the next turn. The loop stops when the verifier says STOP or
     the turn budget is exhausted.

The verifier is loop infrastructure — the planner never calls it.

Run:
    python open-newton/loop/run_loop.py "a steel ball drops onto a table and bounces"

Env (see open-newton/.env): PLANNER_API_KEY / PLANNER_BASE_URL / PLANNER_MODEL /
PLANNER_AUTH (planner), SEEDANCE_API_KEY (video backend), GEMINI_API_KEY (verifier),
SERPER_API_KEY (image_search), IMG_CREATE_* (img_create / make_keyframes).
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from openai import OpenAI

# ---------------------------------------------------------------------------
# Paths: import the self-contained tool cores + the seedance client directly.
# ---------------------------------------------------------------------------
THIS_DIR = Path(__file__).resolve().parent
OPEN_NEWTON = THIS_DIR.parent
TOOLS_DIR = OPEN_NEWTON / "tools"  # tool cores + seedance_run.py / seedance_local.py

if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))
if str(THIS_DIR) not in sys.path:
    sys.path.insert(0, str(THIS_DIR))

from memory import Attempt, Memory  # noqa: E402  (loop/memory.py)

# Every tool ships its own `core.py`; bare `from core import ...` would collide
# on sys.path. Load each core as a uniquely-named module from its file path.
_CORE_CACHE: Dict[str, Any] = {}


def _load_module(name: str, path: Path):
    """Load (and cache) a module from a file path under a unique name."""
    if name in _CORE_CACHE:
        return _CORE_CACHE[name]
    pkg_dir = str(path.parent)
    if pkg_dir not in sys.path:
        sys.path.insert(0, pkg_dir)
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load module: {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    _CORE_CACHE[name] = mod
    return mod


def _tool_core(tool: str):
    """Return the loaded core module for a tool (image / image_search / simulator / video_verifier)."""
    return _load_module(f"on_core_{tool}", TOOLS_DIR / tool / "core.py")


def _load_tool_configs(server_path: Path) -> List[Dict[str, Any]]:
    """Import a tool server.py by path and return its advertised tool_configs.

    The server does ``from core import ...`` at import time; pre-load that
    tool's core under the canonical name ``core`` in sys.modules so the import
    resolves to the right file (not whichever core was imported first).
    """
    tool = server_path.parent.name
    core_mod = _load_module(f"on_core_{tool}", server_path.parent / "core.py")
    sys.modules["core"] = core_mod  # make `from core import X` in server.py resolve here
    spec = importlib.util.spec_from_file_location(f"_cfg_{tool}", server_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load tool server: {server_path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    configs = getattr(mod, "tool_configs", None)
    if not configs:
        raise RuntimeError(f"{server_path} has no tool_configs")
    return configs


# ---------------------------------------------------------------------------
# Tool registry: tool_name -> executor. Executors run the core directly and
# return (text_summary, image_data_urls, staged_updates). staged_updates feed
# the eventual seedance generation (reference images / reference video).
# ---------------------------------------------------------------------------
class Staged:
    """Reference material the tools produced, consumed by the video generator."""

    def __init__(self) -> None:
        self.ref_images: List[str] = []   # local image paths (weak appearance refs)
        self.ref_video: Optional[str] = None  # local sim video path
        # i2v conditioning: a first (and optional last) frame the generated video
        # must START (and END) on — a much STRONGER constraint than a reference
        # image. make_keyframes fills these; img_create can opt in via as_first_frame.
        self.first_frame: Optional[str] = None
        self.last_frame: Optional[str] = None
        # Ground-truth description of the ref video derived from the simulator's
        # scene_spec (object count/material), so the verifier doesn't have to
        # (mis)count the reference clip itself.
        self.ref_video_desc: Optional[str] = None
        # Latest image_search candidates. The planner can inspect thumbnails, then
        # call select_reference_image(index=...) to stage its chosen result.
        self.image_search_results: List[Dict[str, Any]] = []

    def clone(self) -> "Staged":
        """A copy carried into the next turn so already-validated conditioning
        (sim video / keyframes / ref images) persists — the planner amends it
        incrementally instead of rebuilding from scratch each turn."""
        s = Staged()
        s.ref_images = list(self.ref_images)
        s.ref_video = self.ref_video
        s.first_frame = self.first_frame
        s.last_frame = self.last_frame
        s.ref_video_desc = self.ref_video_desc
        # image_search_results are per-turn scratch; do NOT carry them forward.
        return s

    def summary(self) -> str:
        """One-line human description of what conditioning is currently staged,
        shown to the planner so it can decide what to keep / add / replace."""
        bits = []
        if self.ref_video:
            bits.append(f"a simulation reference video ({self.ref_video_desc or 'physics motion'})")
        if self.first_frame:
            bits.append("a first-frame keyframe")
        if self.last_frame:
            bits.append("a last-frame keyframe")
        if self.ref_images:
            bits.append(f"{len(self.ref_images)} reference image(s)")
        return "; ".join(bits) if bits else "nothing yet (text prompt only)"


def _exec_image_search(args: Dict[str, Any], staged: Staged) -> Tuple[str, List[str]]:
    ImageSearchCore = _tool_core("image_search").ImageSearchCore
    recs = ImageSearchCore().search(args.get("query", ""), int(args.get("top_k", 3) or 3))
    if not recs:
        return ("image_search: no downloadable images found.", [])
    staged.image_search_results = recs
    lines = [
        "image_search results (top result is staged by default; call "
        "select_reference_image with an index to choose a different result):"
    ]
    thumbs: List[str] = []
    for r in recs:
        lines.append(f"  [{r['index']}] {r['title'][:60]} -> {r['local_path']}")
        if r.get("thumbnail"):
            thumbs.append(r["thumbnail"])
    staged.ref_images = [recs[0]["local_path"]]
    lines.append(f"(currently staged reference image: index 0 -> {recs[0]['local_path']})")
    return ("\n".join(lines), thumbs)


def _exec_select_reference_image(args: Dict[str, Any], staged: Staged) -> Tuple[str, List[str]]:
    if not staged.image_search_results:
        return ("select_reference_image failed: no image_search results are available this turn.", [])
    try:
        idx = int(args.get("index"))
    except Exception:  # noqa: BLE001
        return ("select_reference_image failed: provide an integer index from image_search.", [])
    for r in staged.image_search_results:
        if int(r.get("index", -1)) == idx:
            staged.ref_images = [r["local_path"]]
            return (f"selected image_search result index {idx}: {r['local_path']} "
                    "(staged as reference image)", [])
    valid = [r.get("index") for r in staged.image_search_results]
    return (f"select_reference_image failed: index {idx} not found. Valid indexes: {valid}", [])


def _exec_img_create(args: Dict[str, Any], staged: Staged) -> Tuple[str, List[str]]:
    ImageCore = _tool_core("image").ImageCore
    recs = ImageCore().generate_or_edit(
        prompt=args.get("prompt", ""),
        image_paths=args.get("reference_images") or [],
    )
    paths = [r["local_path"] for r in recs]
    # By default the image is a weak appearance reference. If the planner asks for
    # `as_first_frame`, stage it as the i2v FIRST FRAME instead — a much stronger
    # constraint that forces the generated video to start on this exact image
    # (e.g. a frame already showing the correct object count/layout).
    if args.get("as_first_frame") and paths:
        staged.first_frame = paths[0]
        return (f"img_create produced: {paths[0]} (staged as the i2v FIRST FRAME — "
                f"the video will start on this exact image)", [])
    staged.ref_images = paths
    return (f"img_create produced: {', '.join(paths)} (staged as reference image)", [])


def _exec_make_keyframes(args: Dict[str, Any], staged: Staged) -> Tuple[str, List[str]]:
    ImageCore = _tool_core("image").ImageCore
    recs = ImageCore().keyframes(
        first_prompt=args.get("first_prompt", ""),
        last_prompt=args.get("last_prompt", ""),
        reference_images=args.get("reference_images") or [],
    )
    paths = [r["local_path"] for r in recs]
    # Keyframes are TRUE i2v conditioning: first frame (and last, if produced) the
    # generated video must begin/end on — far stronger than a reference image.
    if paths:
        staged.first_frame = paths[0]
    if len(paths) > 1:
        staged.last_frame = paths[1]
    return (f"make_keyframes produced first={paths[0] if paths else '?'} "
            f"last={paths[1] if len(paths) > 1 else '?'} (staged as i2v first/last frames — "
            f"the video will start/end on these exact images)", [])


def _describe_scene_spec(spec: Dict[str, Any]) -> str:
    """Ground-truth one-line description of a sim scene from its scene_spec, so
    the verifier knows the true object count/material of the reference clip
    instead of (mis)counting it visually. Groups movable bodies by
    (material, asset/type) and lists counts; ignores fixed supports/floor."""
    from collections import Counter
    counts: "Counter[str]" = Counter()
    for o in spec.get("objects", []):
        if o.get("fixed") or o.get("type") == "plane":
            continue
        mat = o.get("material", "rigid")
        kind = o.get("asset") or o.get("type", "object")
        counts[f"{mat} {kind}"] += 1
    if not counts:
        return ""
    parts = [f"{n} {label}" + ("s" if n > 1 else "") for label, n in counts.items()]
    return "a physics simulation containing exactly " + ", ".join(parts)


def _exec_simulate(args: Dict[str, Any], staged: Staged) -> Tuple[str, List[str]]:
    SimCore = _tool_core("simulator").SimCore
    spec = args.get("scene_spec") or args  # tolerate flat or nested
    result = SimCore().run(spec)
    vp = result.get("video_path")
    if vp:
        staged.ref_video = vp
        staged.ref_video_desc = _describe_scene_spec(spec)
    summary = result.get("summary", "")
    return (f"simulate: {summary}\nreference video (staged): {vp}", [])


# Map tool name -> (executor, server.py path for its tool_configs)
_TOOL_SPECS: Dict[str, Tuple[Callable[..., Tuple[str, List[str]]], Path]] = {
    "image_search": (_exec_image_search, TOOLS_DIR / "image_search" / "server.py"),
    "select_reference_image": (_exec_select_reference_image, TOOLS_DIR / "image_search" / "server.py"),
    "img_create": (_exec_img_create, TOOLS_DIR / "image" / "server.py"),
    "make_keyframes": (_exec_make_keyframes, TOOLS_DIR / "image" / "server.py"),
    "simulate": (_exec_simulate, TOOLS_DIR / "simulator" / "server.py"),
}


def build_tool_configs() -> List[Dict[str, Any]]:
    """Collect tool_configs from the tool servers (dedup by function name)."""
    seen: set[str] = set()
    out: List[Dict[str, Any]] = []
    for server in (TOOLS_DIR / "image" / "server.py",
                   TOOLS_DIR / "image_search" / "server.py",
                   TOOLS_DIR / "simulator" / "server.py"):
        for cfg in _load_tool_configs(server):
            name = cfg.get("function", {}).get("name")
            if name and name in _TOOL_SPECS and name not in seen:
                seen.add(name)
                out.append(cfg)
    return out


def execute_tool(name: str, args: Dict[str, Any], staged: Staged) -> Tuple[str, List[str]]:
    spec = _TOOL_SPECS.get(name)
    if spec is None:
        return (f"Unknown tool: {name}", [])
    executor = spec[0]
    try:
        return executor(args, staged)
    except Exception as exc:  # noqa: BLE001
        return (f"{name} failed: {exc}", [])


# ---------------------------------------------------------------------------
# Progressive skill disclosure (Claude Code style): up front the planner only
# sees each skill's name + description; it pulls the full body on demand via the
# read_skill tool. Skills carry the "how to use a tool well" domain knowledge
# (e.g. simulator.md's guidance on filling in scene/lighting when using a sim
# reference video) that is too long to keep in the system prompt.
# ---------------------------------------------------------------------------
SKILLS_DIR = OPEN_NEWTON / "planner_skills"


def _parse_frontmatter(text: str) -> Tuple[Dict[str, str], str]:
    """Return (frontmatter dict, body) for a markdown file with YAML frontmatter."""
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            fm_block = text[3:end].strip()
            body = text[end + 4:].lstrip("\n")
            fm: Dict[str, str] = {}
            for line in fm_block.splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    fm[k.strip()] = v.strip()
            return fm, body
    return {}, text


def load_skill_catalog() -> Dict[str, Dict[str, str]]:
    """Map skill name -> {description, body, path} for every planner_skills/*.md."""
    catalog: Dict[str, Dict[str, str]] = {}
    if not SKILLS_DIR.is_dir():
        return catalog
    for md in sorted(SKILLS_DIR.glob("*.md")):
        fm, body = _parse_frontmatter(md.read_text(encoding="utf-8"))
        name = fm.get("name") or md.stem
        catalog[name] = {
            "description": fm.get("description", ""),
            "body": body,
            "path": str(md),
        }
    return catalog


def render_skill_index(catalog: Dict[str, Dict[str, str]]) -> str:
    """One-line-per-skill catalog for the system prompt (name + description)."""
    lines = ["Available skills (call read_skill with the name to load full guidance):"]
    for name, meta in catalog.items():
        lines.append(f"- {name}: {meta['description']}")
    return "\n".join(lines)


SELECT_REFERENCE_IMAGE_CONFIG: Dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "select_reference_image",
        "description": (
            "After image_search returns thumbnail candidates, choose which result "
            "index should be staged as the video reference image. Use this when "
            "a non-zero image_search result is visually better than the default "
            "top result."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "index": {
                    "type": "integer",
                    "description": "The image_search result index to stage as the reference image.",
                },
            },
            "required": ["index"],
        },
    },
}


READ_SKILL_CONFIG: Dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "read_skill",
        "description": (
            "Load the full guidance document for a skill by name (progressive "
            "disclosure: you start with only names + descriptions). Read the "
            "relevant skill BEFORE using its tool — e.g. read 'simulator' before "
            "calling simulate, 'prompt_refine' before writing the final prompt."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "The skill name to load."},
            },
            "required": ["name"],
        },
    },
}


SYSTEM_PROMPT = (
    "You are the Planner of OpenNewton, a physics-grounded video generation "
    "system. Given a physics scenario, produce the best possible `video_prompt` "
    "(a vivid, concrete caption) for a video generator, optionally backed by "
    "reference material from your tools (image_search, select_reference_image, "
    "img_create, make_keyframes, simulate). You start with only skill NAMES + descriptions "
    "(listed below); before using a tool, call read_skill(name) to load its full "
    "guidance — each tool's skill holds critical do's and don'ts, so read the "
    "relevant one first. Call tools as needed; when the material is ready, reply "
    "with PLAIN TEXT (no tool call) and that text becomes the final video_prompt "
    "(the system then generates the video and a verifier scores it, feeding back "
    "for another round if needed).\n"
    "CRITICAL OUTPUT RULE: your final plain-text answer must be ONLY the "
    "video_prompt itself — a clean scene description. Do NOT include any reasoning, "
    "commentary, planning notes, or <think>...</think> tags. Whatever you reply IS "
    "sent verbatim to the video generator, so anything that is not the prompt "
    "corrupts it.\n\n"
    "ITERATION STRATEGY — you own the decision of WHAT to change each round. "
    "After a generation you get back ONLY objective results: a blind A/B score vs "
    "the baseline (semantic & physical) and what is still wrong. No tool is "
    "recommended to you — diagnose it yourself.\n\n"
    "CONDITIONING CARRIES OVER. The references you already staged (a sim video, "
    "keyframes, reference images) are KEPT into the next round — you start each "
    "round with whatever the last generation used, NOT a blank slate. So change "
    "INCREMENTALLY: keep what works and amend only the part that is wrong. "
    "Calling a tool REPLACES the piece it produces (e.g. `simulate` overwrites the "
    "sim video, `make_keyframes` overwrites the keyframes); NOT calling that tool "
    "KEEPS the existing piece. Never rebuild everything from scratch, and never "
    "drop a good reference just to retry with text only.\n"
    "Use these principles:\n"
    "- If the conditioning PASSED the pre-check (e.g. the simulation already shows "
    "the correct motion / object count) but the GENERATED video still gets it "
    "wrong (wrong count, wrong action), the simulator is NOT the problem — the "
    "generator failed to follow it. Re-running simulate will not help. KEEP the "
    "sim video and ADD a STRONGER visual constraint on top: use `img_create` / "
    "`make_keyframes` to build a first frame that already shows the correct scene "
    "(exact count/layout), and/or reference the sim video more explicitly and "
    "reword the prompt.\n"
    "- If the MOTION / physics itself is wrong and you have no physics reference "
    "yet, use `simulate` to get a physics-correct reference video.\n"
    "- If a specific real object/character/place looks wrong, use `image_search` "
    "to inspect real candidates, `select_reference_image` if a non-default "
    "candidate is best, or `img_create` (synthesize/edit) to fix its appearance.\n"
    "- If only the wording is weak (the references are fine), just rewrite the "
    "prompt — no tool call needed, the staged references stay.\n"
    "Look at your own history: if a tool already produced correct conditioning, "
    "don't repeat it — keep it and change a DIFFERENT part of the pipeline."
)


def call_planner(
    client: OpenAI,
    model: str,
    messages: List[Dict[str, Any]],
    tool_configs: List[Dict[str, Any]],
    max_completion_tokens: int = 4096,
):
    """One planner completion. gpt-5 family needs max_completion_tokens; default
    to the 'high' reasoning effort for the planner's tool-use decisions."""
    kwargs: Dict[str, Any] = dict(
        model=model, messages=messages, tools=tool_configs, tool_choice="auto",
        temperature=0, reasoning_effort="high",
    )
    try:
        return client.chat.completions.create(max_completion_tokens=max_completion_tokens, **kwargs)
    except Exception as exc:  # noqa: BLE001
        if "max_completion_tokens" in str(exc) or "max_tokens" in str(exc):
            return client.chat.completions.create(max_tokens=max_completion_tokens, **kwargs)
        raise


_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def _strip_think(text: str) -> str:
    """Remove any leaked reasoning the planner wrote into its final answer.

    gpt-5.x sometimes emits its chain-of-thought as ``<think>...</think>`` (or an
    unclosed ``<think>`` tail) inside the content. That text must never reach the
    video generator or the condition judge, so we drop closed blocks, then any
    dangling ``<think>`` and everything after it, and finally stray tag markers.
    """
    if not text:
        return text
    t = _THINK_RE.sub("", text)
    low = t.lower()
    i = low.rfind("<think>")
    if i != -1:  # unclosed tag — drop it and the trailing reasoning
        t = t[:i]
    return t.replace("</think>", "").strip()


def planner_prepare_turn(
    client: OpenAI,
    model: str,
    messages: List[Dict[str, Any]],
    tool_configs: List[Dict[str, Any]],
    staged: Staged,
    tool_log: List[Dict[str, Any]],
    skill_catalog: Dict[str, Dict[str, str]],
    max_tool_calls: int = 8,
    verbose: bool = True,
) -> str:
    """Run the planner until it returns plain text (the video_prompt).

    Executes any tool calls in between (GenEvolve-style), feeding results back.
    Appends each tool call (name/args/result) to ``tool_log`` for the trace.
    Returns the final plain-text video_prompt.
    """
    for _ in range(max_tool_calls + 1):
        resp = call_planner(client, model, messages, tool_configs)
        msg = resp.choices[0].message
        tool_calls = msg.tool_calls or []
        if not tool_calls:
            text = _strip_think(msg.content or "")
            messages.append({"role": "assistant", "content": text})
            return text

        # Record the assistant turn that requested tools.
        messages.append({
            "role": "assistant",
            "content": msg.content or "",
            "tool_calls": [tc.model_dump() for tc in tool_calls],
        })
        for tc in tool_calls:
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments or "{}")
            except Exception:  # noqa: BLE001
                args = {}
            if verbose:
                print(f"  [tool] {name}({json.dumps(args, ensure_ascii=False)[:120]})")
            if name == "read_skill":
                sk = skill_catalog.get(str(args.get("name", "")).strip())
                text = sk["body"] if sk else f"Unknown skill: {args.get('name')}"
                thumbs = []
            else:
                text, thumbs = execute_tool(name, args, staged)
            tool_log.append({"tool": name, "args": args, "result": text[:500]})
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": text})
            # Surface image thumbnails so the multimodal planner can SEE candidates.
            if thumbs:
                blocks: List[Dict[str, Any]] = [
                    {"type": "text", "text": "Tool image previews (in result order):"}
                ]
                for t in thumbs:
                    blocks.append({"type": "image_url", "image_url": {"url": t}})
                messages.append({"role": "user", "content": blocks})

    # Tool-call budget exhausted: force a final text answer.
    messages.append({"role": "user", "content":
                     "Stop calling tools. Reply now with ONLY the final video_prompt text."})
    resp = call_planner(client, model, messages, tool_configs)
    text = _strip_think(resp.choices[0].message.content or "")
    messages.append({"role": "assistant", "content": text})
    return text


def generate_video(
    video_prompt: str,
    staged: Staged,
    out_dir: str,
    duration: int = 5,
    resolution: str = "720p",
    ratio: str = "16:9",
    poll_timeout: int = 1800,
) -> str:
    """Generate a video with seedance from the prompt + staged references.

    The video generator's THREE image scenarios are mutually exclusive:
    first-frame / first+last-frame / reference-images cannot be mixed. A reference
    VIDEO is an independent channel that combines with reference images.

    So the staged first/last frame is used as a true i2v keyframe ONLY when it is
    the lone visual condition (no sim reference video). If a sim reference video
    is also present, the first/last frame would collide with it, so we DEMOTE the
    frame(s) to reference images and send them alongside the reference video as a
    multimodal-reference request.
    """
    import seedance_run as sr  # type: ignore
    from seedance_local import to_data_url  # type: ignore

    has_video = bool(staged.ref_video)
    keyframes = [p for p in (staged.first_frame, staged.last_frame) if p]
    if keyframes and has_video:
        # Collision: a reference video is present, so the keyframes can't use the
        # first/last-frame scenario. Demote them to reference images.
        first_url = last_url = None
        ref_image_paths = keyframes + list(staged.ref_images)
        print("  [generate] sim reference video present → demoting first/last frame "
              "to reference image(s) (image scenarios are mutually exclusive)")
    else:
        # No video: use the keyframes as a real first/last-frame i2v condition.
        first_url = to_data_url(staged.first_frame) if staged.first_frame else None
        last_url = to_data_url(staged.last_frame) if staged.last_frame else None
        ref_image_paths = list(staged.ref_images)

    content = sr.build_content(
        text=video_prompt or None,
        first_frame=first_url,
        last_frame=last_url,
        reference_images=[to_data_url(p) for p in ref_image_paths],
        reference_videos=[to_data_url(staged.ref_video)] if staged.ref_video else [],
    )
    task_id = sr.submit(content, resolution=resolution, ratio=ratio, duration=duration)
    return sr.poll(task_id, out_dir, timeout=poll_timeout)


def verify_video_relative(candidate_path: str, baseline_path: str, question: str,
                          seed: Optional[int] = None) -> Dict[str, Any]:
    """Blind A/B: score the candidate video against the fixed baseline.

    The verifier sees both clips in a randomized order, labeled only Video 1 /
    Video 2 (it is never told which is the baseline), and returns a signed score
    in [-10, +10] already remapped to candidate-vs-baseline. score >= REL_STOP (5)
    means the candidate clearly beats the baseline (conclusion STOP).
    """
    GeminiVerifierCore = _tool_core("video_verifier").GeminiVerifierCore
    return GeminiVerifierCore().verify_relative(candidate_path, baseline_path, question, seed=seed)


def judge_condition(baseline_path: str, question: str, staged: "Staged",
                    video_prompt: str, history: Optional[str] = None) -> Dict[str, Any]:
    """Pre-generation check: is the planner's proposed conditioning sound enough
    to plausibly beat the baseline? Returns {reasonable, reason, suggestions}."""
    # Show the judge every proposed visual: i2v first/last frames count as
    # conditioning too (and are the strongest), so include them with the refs.
    judge_images = [p for p in (staged.first_frame, staged.last_frame) if p]
    judge_images += list(staged.ref_images)
    GeminiVerifierCore = _tool_core("video_verifier").GeminiVerifierCore
    return GeminiVerifierCore().judge_condition(
        baseline_path, question,
        ref_video=staged.ref_video, ref_images=judge_images,
        video_prompt=video_prompt, history=history,
    )


def _slug(text: str, n: int = 40) -> str:
    s = "".join(c if c.isalnum() else "_" for c in text.lower()).strip("_")
    return (s[:n] or "run").rstrip("_")


def _archive_into(src: Optional[str], dst_dir: str, prefix: str = "ref_") -> Optional[str]:
    """Copy a reference file into the turn dir; return the archived path (or None)."""
    if not src or not os.path.isfile(src):
        return None
    dst = os.path.join(dst_dir, prefix + os.path.basename(src))
    try:
        shutil.copy2(src, dst)
        return dst
    except Exception:  # noqa: BLE001
        return None


def run_loop(
    question: str,
    max_turns: int = 8,
    out_dir: Optional[str] = None,
    duration: int = 5,
    run_id: Optional[str] = None,
    baseline_override: Optional[str] = None,
    verbose: bool = True,
) -> Dict[str, Any]:
    """Full inference loop. Returns the best video + per-turn history.

    Each run writes a self-contained directory under ``out_dir``:
        <out_dir>/<run_id>/
            trace.json      # full trace: per-turn tool calls, prompt, video, verdict
            turn_N/<id>.mp4 # the video generated that turn
    """
    base = out_dir or str(OPEN_NEWTON / "outputs" / "loop")
    run_id = run_id or f"{_slug(question)}_{os.getpid()}"
    out_dir = os.path.join(base, run_id)
    os.makedirs(out_dir, exist_ok=True)

    api_key = os.environ.get("PLANNER_API_KEY") or os.environ.get("IMG_CREATE_API_KEY", "")
    base_url = os.environ.get("PLANNER_BASE_URL") or os.environ.get("IMG_CREATE_BASE_URL", "")
    model = os.environ.get("PLANNER_MODEL", "gpt-5.5")
    auth = os.environ.get("PLANNER_AUTH", "api-key")
    if not api_key or not base_url:
        raise RuntimeError("PLANNER_API_KEY / PLANNER_BASE_URL not set (see open-newton/.env)")
    headers = {"api-key": api_key} if auth == "api-key" else None
    client = OpenAI(base_url=base_url, api_key=api_key, default_headers=headers)

    skill_catalog = load_skill_catalog()
    tool_configs = build_tool_configs() + [SELECT_REFERENCE_IMAGE_CONFIG, READ_SKILL_CONFIG]
    system_prompt = SYSTEM_PROMPT + "\n\n" + render_skill_index(skill_catalog)
    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"Scenario: {question}"},
    ]

    memory = Memory(question)
    best: Optional[Dict[str, Any]] = None
    trace_path = os.path.join(out_dir, "trace.json")

    # --- Baseline A: a text-to-video clip from the RAW scenario, no tools, no
    # references. It is the fixed zero point every turn's candidate is judged
    # against (blind A/B). Generate it once, up front.
    baseline_dir = os.path.join(out_dir, "baseline")
    os.makedirs(baseline_dir, exist_ok=True)
    baseline_video: Optional[str] = None
    if baseline_override:
        # Reuse a pre-existing baseline clip instead of regenerating it (saves a
        # seedance call / avoids a transient baseline-generation failure). Copy it
        # into this run's baseline dir so the run stays self-contained.
        if not os.path.isfile(baseline_override):
            raise RuntimeError(f"baseline_override not found: {baseline_override}")
        baseline_video = os.path.join(baseline_dir, os.path.basename(baseline_override))
        shutil.copy2(baseline_override, baseline_video)
        if verbose:
            print(f"\n{'='*60}\n[Baseline] reusing existing baseline: {baseline_override}\n{'='*60}")
    else:
        if verbose:
            print(f"\n{'='*60}\n[Baseline] generating reference video from raw scenario...\n{'='*60}")
        try:
            baseline_video = generate_video(question, Staged(), baseline_dir, duration=duration)
            if verbose:
                print(f"  [baseline] {baseline_video}")
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"baseline video generation failed: {exc}") from exc

    # STOP when the gemini blind-A/B relative score (candidate vs baseline) >=
    # REL_STOP.
    REL_STOP = 5

    def _write_trace() -> None:
        trace = {
            "model": model,
            "max_turns": max_turns,
            "best_video": best["video_path"] if best else None,
            "best_score": best["key"] if best else None,
            "best_turn": best["turn"] if best else None,
            **memory.to_trace(),
        }
        with open(trace_path, "w", encoding="utf-8") as f:
            json.dump(trace, f, ensure_ascii=False, indent=2)

    # --- turn0: the baseline is the bar to beat. It is NOT scored on its own
    # (the only scorer is the gemini blind A/B, which needs a candidate to compare
    # against), so there is no turn0 early stop — the loop always proceeds to the
    # first condition turn.
    memory.set_baseline(baseline_video)

    # --- State machine. Each iteration is ONE turn (a condition pre-check turn OR
    # a generation turn). "need_condition": planner prepares conditioning, gemini
    # judges if it is sound enough to beat the baseline; if yes we advance to
    # "generate", else the planner revises next turn. "generate": run seedance,
    # score with the gemini blind A/B (relative) verifier.
    state = "need_condition"
    pending: Optional[Tuple[int, str, Staged]] = None
    # The conditioning that the LAST generation actually used. Each new condition
    # turn starts from a CLONE of this (not a blank Staged), so already-validated
    # references (sim video / keyframes) persist and the planner amends them
    # incrementally instead of rebuilding — or silently dropping — them.
    committed = Staged()

    def _best_key(rel: int) -> List[int]:
        return [rel]

    for turn in range(1, max_turns + 1):
        if state == "need_condition":
            if verbose:
                print(f"\n{'='*60}\n[Turn {turn}/{max_turns}] planner preparing condition...\n{'='*60}")
            staged = committed.clone()
            # Tell the planner what conditioning it already has in hand, so it can
            # KEEP what works and change only one part (reword the prompt, add a
            # keyframe on top of the sim video, etc.) instead of starting over.
            messages.append({"role": "user", "content":
                             f"Conditioning currently staged (carried from the last generation): "
                             f"{staged.summary()}. It is already in hand — you do NOT need to "
                             f"re-create it. Keep what works; change only what the feedback says is "
                             f"wrong (reword the prompt, and/or ADD a keyframe/reference on top, "
                             f"and/or re-make only the broken piece). A tool call REPLACES that "
                             f"piece; not calling a tool KEEPS it."})
            tool_log: List[Dict[str, Any]] = []
            video_prompt = planner_prepare_turn(
                client, model, messages, tool_configs, staged, tool_log, skill_catalog,
                verbose=verbose,
            )
            if verbose:
                print(f"  [video_prompt] {video_prompt[:160]}")
                print(f"  [staged] first_frame={staged.first_frame} last_frame={staged.last_frame} "
                      f"ref_images={staged.ref_images} ref_video={staged.ref_video}")

            # Pre-generation check: is this conditioning worth a generation?
            # The judge gets the FULL run history (baseline + every prior attempt,
            # including rejected conditions) so it can refuse a repeated dead end.
            try:
                judge = judge_condition(baseline_video, question, staged, video_prompt,
                                        history=memory.for_condition_judge())
            except Exception as exc:  # noqa: BLE001
                judge = {"reasonable": True, "reason": f"(pre-check skipped: {exc})", "suggestions": []}
            if verbose:
                print(f"  [condition] reasonable={judge['reasonable']} — {judge.get('reason','')}")

            # Archive this turn's proposed conditioning into its own turn dir so
            # the run is self-contained — ESPECIALLY for rejected conditions: the
            # sim/ref material otherwise only lives in the global outputs/sim and
            # gets overwritten/orphaned, leaving no way to inspect what was refused.
            turn_dir = os.path.join(out_dir, f"turn_{turn}")
            os.makedirs(turn_dir, exist_ok=True)
            archived_video = _archive_into(staged.ref_video, turn_dir) if staged.ref_video else None
            archived_images = [_archive_into(p, turn_dir) for p in staged.ref_images]
            archived_first = _archive_into(staged.first_frame, turn_dir, "first_frame_")
            archived_last = _archive_into(staged.last_frame, turn_dir, "last_frame_")

            memory.add(Attempt(
                turn=turn, kind="condition", tool_calls=tool_log,
                video_prompt=video_prompt, ref_images=list(staged.ref_images),
                first_frame=staged.first_frame, last_frame=staged.last_frame,
                ref_video=staged.ref_video, ref_video_desc=staged.ref_video_desc,
                archived_ref_video=archived_video, archived_ref_images=archived_images,
                archived_first_frame=archived_first, archived_last_frame=archived_last,
                condition_judge=judge,
            ))
            _write_trace()

            # Carry this turn's conditioning forward REGARDLESS of the verdict, so
            # references the planner built here (a sim video / keyframes) are not
            # lost if the condition is rejected. The next condition turn clones
            # this; the planner then amends only what the rejection feedback flags,
            # instead of rebuilding from scratch (or silently dropping a good sim).
            committed = staged.clone()
            if judge["reasonable"]:
                pending = (turn, video_prompt, staged)
                state = "generate"
            else:
                fb = ("Condition pre-check (before generating): the proposed conditioning is NOT "
                      f"sound enough to beat the baseline. Reason: {judge.get('reason','')}. "
                      f"Fix: {judge.get('suggestions')}. Revise the conditioning (re-simulate / "
                      "re-image / reword) and reply again — no video was generated yet.")
                messages.append({"role": "user", "content": fb})
            continue

        # state == "generate"
        assert pending is not None
        condition_turn, video_prompt, staged = pending
        pending = None
        if verbose:
            print(f"\n{'='*60}\n[Turn {turn}/{max_turns}] generating video...\n{'='*60}")

        turn_dir = os.path.join(out_dir, f"turn_{turn}")
        os.makedirs(turn_dir, exist_ok=True)
        archived_video = _archive_into(staged.ref_video, turn_dir) if staged.ref_video else None
        archived_images = [_archive_into(p, turn_dir) for p in staged.ref_images]
        archived_first = _archive_into(staged.first_frame, turn_dir, "first_frame_")
        archived_last = _archive_into(staged.last_frame, turn_dir, "last_frame_")
        rec = memory.add(Attempt(
            turn=turn, kind="generate", condition_source_turn=condition_turn,
            video_prompt=video_prompt, ref_images=list(staged.ref_images),
            first_frame=staged.first_frame, last_frame=staged.last_frame,
            ref_video=staged.ref_video, ref_video_desc=staged.ref_video_desc,
            archived_ref_images=archived_images, archived_ref_video=archived_video,
            archived_first_frame=archived_first, archived_last_frame=archived_last,
        ))
        try:
            video_path = generate_video(video_prompt or question, staged, turn_dir, duration=duration)
            rec.video_path = video_path
            # This conditioning was actually generated from — carry it into the
            # next condition turn so the planner amends it rather than rebuilding.
            committed = staged.clone()
        except Exception as exc:  # noqa: BLE001
            rec.error = f"video generation failed: {exc}"
            _write_trace()
            if verbose:
                print(f"  [seedance] FAILED: {exc}")
            messages.append({"role": "user", "content":
                             f"Video generation failed: {exc}. Adjust the prompt (avoid copyrighted "
                             "characters / unsafe content) and try again."})
            state = "need_condition"
            continue

        # Relative blind-A/B (gemini): candidate vs the text-only baseline.
        verdict = verify_video_relative(video_path, baseline_video, question, seed=turn)
        rel = verdict["score"]
        rec.rel_verdict = verdict
        rec.rel_score = rel
        if verbose:
            print(f"  [rel A/B] score={rel:+d} vs baseline (order: {verdict.get('_order')})")
            print(f"  [rel A/B] {verdict.get('summary','')}")

        key = _best_key(rel)
        if best is None or key > best["key"]:
            best = {"key": key, "video_path": video_path, "turn": turn,
                    "verdict": {"rel": verdict}}

        stop = rel >= REL_STOP
        if stop:
            rec.stop_reason = f"blind-A/B rel={rel:+d}>={REL_STOP}"
        else:
            rec.stop_reason = f"blind-A/B rel={rel:+d} (need >={REL_STOP}) — not met"
        rec.stop = stop
        _write_trace()

        if stop:
            if verbose:
                print(f"\n[loop] STOP at turn {turn} — rel={rel:+d}")
            break

        # Feed back ONLY the objective result (scores + what's wrong). It is up to
        # YOU (the planner) to decide what to change next — which tool, which
        # conditioning — using the strategy guidance in your system prompt.
        used = []
        if staged.ref_video:
            used.append("a simulation reference video")
        if staged.ref_images:
            used.append(f"{len(staged.ref_images)} reference image(s)")
        used_str = " and ".join(used) if used else "a text prompt only"
        fb = (f"Result of this generation (conditioned on {used_str}, which passed the "
              f"pre-check) — Blind A/B vs the text-only baseline: rel={rel:+d} (need >= {REL_STOP} to stop). "
              f"What is still wrong: semantic — {verdict.get('sa_note','')} physics — "
              f"{verdict.get('pc_note','')} {verdict.get('issues')}. "
              f"Decide your own next move (rewording, a different tool, or different "
              f"conditioning) per your strategy, then reply with the new prompt.")
        messages.append({"role": "user", "content": fb})
        state = "need_condition"

    _write_trace()
    n_turns = len(memory.attempts)
    result = {
        "question": question,
        "baseline_video": baseline_video,
        "best_video": best["video_path"] if best else None,
        "best_score": best["key"] if best else None,
        "best_turn": best["turn"] if best else None,
        "turns": n_turns,
        "trace_path": trace_path,
        "memory": memory.to_trace(),
    }
    if verbose:
        print(f"\n{'='*60}")
        print(f"[loop] done — {n_turns} turn(s). best={result['best_video']} "
              f"(score={result['best_score']}, turn={result['best_turn']})")
        print(f"[loop] trace: {trace_path}")
        print(f"{'='*60}")
    return result


def main() -> None:
    ap = argparse.ArgumentParser(description="OpenNewton inference loop")
    ap.add_argument("question", help="the physics scenario to turn into a video")
    ap.add_argument("--max-turns", type=int, default=8)
    ap.add_argument("--duration", type=int, default=5, help="video seconds")
    ap.add_argument("--out-dir", default=None)
    ap.add_argument("--baseline", default=None,
                    help="path to an existing baseline mp4 to reuse (skip baseline generation)")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()
    result = run_loop(
        args.question,
        max_turns=args.max_turns,
        out_dir=args.out_dir,
        duration=args.duration,
        baseline_override=args.baseline,
        verbose=not args.quiet,
    )
    print(json.dumps({k: v for k, v in result.items() if k != "history"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

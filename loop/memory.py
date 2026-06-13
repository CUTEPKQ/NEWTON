"""Loop memory — the single source of truth for one OpenNewton run.

Every turn writes one record here; every consumer reads a *view* of it:

  - the **planner** keeps its own running chat (`messages`) and is not driven by
    this object — but its turns are mirrored here for the trace.
  - the **condition pre-check** (gemini ``judge_condition``) reads the FULL
    recap: the baseline + every prior attempt, INCLUDING rejected conditions
    and why they were rejected, so it can refuse a proposal that repeats a known
    dead end.
  - the **blind A/B scorer** deliberately gets NO history (it must judge the two
    clips on what it sees, uninfluenced by which attempt this is).
  - ``to_trace()`` serializes everything for ``trace.json``.

One write site, three read views: new fields only touch this file.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class Attempt:
    """One turn of the loop — either a condition pre-check or a generation."""

    turn: int
    kind: str  # "condition" | "generate"
    video_prompt: str = ""
    ref_video: Optional[str] = None
    ref_images: List[str] = field(default_factory=list)
    first_frame: Optional[str] = None  # i2v first frame (strong constraint)
    last_frame: Optional[str] = None   # i2v last frame
    ref_video_desc: Optional[str] = None  # ground-truth content of the sim clip
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    # condition turns:
    condition_judge: Optional[Dict[str, Any]] = None  # {reasonable, reason, suggestions}
    # generate turns:
    condition_source_turn: Optional[int] = None
    video_path: Optional[str] = None
    archived_ref_video: Optional[str] = None
    archived_ref_images: List[str] = field(default_factory=list)
    archived_first_frame: Optional[str] = None
    archived_last_frame: Optional[str] = None
    rel_verdict: Optional[Dict[str, Any]] = None      # full blind-A/B verdict
    rel_score: Optional[int] = None
    stop: Optional[bool] = None          # did this generation trigger the loop to STOP?
    stop_reason: Optional[str] = None    # human-readable why (which threshold)
    error: Optional[str] = None


class Memory:
    """Single source of truth for a run: baseline + ordered attempts."""

    def __init__(self, question: str) -> None:
        self.question = question
        self.baseline_video: Optional[str] = None
        self.attempts: List[Attempt] = []

    # ---- writes ----
    def set_baseline(self, video: Optional[str]) -> None:
        self.baseline_video = video

    def add(self, attempt: Attempt) -> Attempt:
        self.attempts.append(attempt)
        return attempt

    # ---- read views ----
    def for_condition_judge(self) -> str:
        """FULL recap for the gemini condition pre-check.

        Includes EVERY prior attempt — both generations (prompt + how it scored
        vs the baseline + what was wrong) and rejected conditions (prompt + why it
        was rejected) — so the judge can spot a proposal that repeats a path
        already shown not to work.
        """
        lines: List[str] = []
        lines.append("BASELINE: a text-only video of the scenario is the bar to beat. "
                     "A new condition is only worth generating if it is likely to clearly "
                     "improve on it.")

        for a in self.attempts:
            if a.kind == "condition":
                j = a.condition_judge or {}
                if j.get("reasonable") is False:
                    sugg = ", ".join(j.get("suggestions") or [])
                    lines.append(
                        f"- turn {a.turn}: a condition was REJECTED before generating. "
                        f"Proposed prompt: {(a.video_prompt or '')[:160]}. "
                        f"Rejected because: {j.get('reason','')}."
                        + (f" Suggested fixes were: {sugg}." if sugg else ""))
            elif a.kind == "generate":
                if a.error:
                    lines.append(f"- turn {a.turn}: generation FAILED ({a.error}).")
                    continue
                v = a.rel_verdict or {}
                problems = "; ".join(filter(None, [
                    v.get("sa_note", ""), v.get("pc_note", ""),
                    ", ".join(v.get("issues") or []),
                ]))
                score_bits = []
                if a.rel_score is not None:
                    score_bits.append(f"blind-A/B rel={a.rel_score:+d} vs baseline")
                lines.append(
                    f"- turn {a.turn}: GENERATED a video. "
                    f"Prompt: {(a.video_prompt or '')[:160]}. "
                    + ("Scored " + ", ".join(score_bits) + ". " if score_bits else "")
                    + (f"What was still wrong: {problems}." if problems else ""))

        if not lines:
            return ""
        return ("History of this run so far (use it to avoid re-proposing a condition that "
                "already failed or that does not fix a known problem):\n" + "\n".join(lines))

    @staticmethod
    def _attempt_trace(a: "Attempt") -> Dict[str, Any]:
        """One attempt as a grouped, readable record:

          turn / kind
          planner    : what the planner did (tools it called + the prompt it wrote)
          condition  : the staged conditioning material
          generation : the produced video + archived material (generate turns)
          verifier   : condition pre-check or generation scorers + STOP decision
        """
        rec: Dict[str, Any] = {"turn": a.turn, "kind": a.kind}
        if a.error:
            rec["error"] = a.error

        # --- planner: tools called + the prompt produced ---
        rec["planner"] = {
            "tools_called": [tc.get("tool") for tc in a.tool_calls],
            "tool_calls": a.tool_calls,
            "video_prompt": a.video_prompt,
        }

        # --- conditioning material staged this turn ---
        cond: Dict[str, Any] = {
            "ref_video": a.ref_video,
            "ref_video_desc": a.ref_video_desc,
            "ref_images": a.ref_images,
            "first_frame": a.first_frame,
            "last_frame": a.last_frame,
            "archived_ref_video": a.archived_ref_video,
            "archived_ref_images": a.archived_ref_images,
            "archived_first_frame": a.archived_first_frame,
            "archived_last_frame": a.archived_last_frame,
        }
        rec["condition"] = cond

        # --- verifier output ---
        if a.kind == "condition":
            rec["verifier"] = {
                "pre_check": a.condition_judge,  # {reasonable, reason, suggestions}
            }
        elif a.kind == "generate":
            rec["generation"] = {
                "condition_source_turn": a.condition_source_turn,
                "video_path": a.video_path,
            }
            v = a.rel_verdict or {}
            rec["verifier"] = {
                "gemini_rel": {
                    "score": a.rel_score,
                    "sa_note": v.get("sa_note"),
                    "pc_note": v.get("pc_note"),
                    "issues": v.get("issues"),
                    "summary": v.get("summary"),
                    "order": v.get("_order"),
                },
                "stop": a.stop,
                "stop_reason": a.stop_reason,
            }
        return rec

    def to_trace(self) -> Dict[str, Any]:
        return {
            "question": self.question,
            "baseline_video": self.baseline_video,
            "attempts": [self._attempt_trace(a) for a in self.attempts],
        }

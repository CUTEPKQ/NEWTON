"""Video scoring for the OpenNewton loop.

  - ``GeminiVerifierCore``: a multimodal LLM (Gemini, via a Gemini-compatible
    ``generateContent`` gateway) used for two jobs —
      * ``verify_relative``: blind A/B — watches the candidate and the baseline in
        randomized order (never told which is which) and scores how much better the
        candidate is, in [-10, +10].
      * ``judge_condition``: pre-generation check — given the baseline plus the
        planner's proposed conditioning (sim video / reference images / prompt),
        judges whether it is sound enough to be worth a generation.

The Gemini side speaks ``POST {base_url}/v1beta/models/{model}:generateContent``
with a ``contents`` array carrying ``inline_data`` (base64 mp4) parts plus a text
instruction. Model, base url and api key are configuration (env vars or kwargs).
"""

from __future__ import annotations

import base64
import json
import os
import random
import re
import time
from pathlib import Path
from typing import Any, Dict, Optional

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None  # type: ignore


DEFAULT_BASE_URL = "https://lk.lingkeapi.cn"
DEFAULT_MODEL = "gemini-3.1-flash-lite"
# Blind A/B stop threshold: the candidate must beat the baseline by at least this
# many points (signed score in [-10, +10]) for the loop to STOP.
REL_STOP_THRESHOLD = 5
# Gemini inline_data must stay within the request body limit; videos above this
# should be hosted and passed by URL instead (not implemented here).
MAX_INLINE_MB = 18.0


# Relative verifier (BLIND A/B test): the judge sees two clips of the same
# scenario in a RANDOMIZED order, labeled only "Video 1" / "Video 2" — it is NOT
# told which one is the baseline or which is the candidate, so it cannot be
# biased toward "ours". It scores how much better Video 2 is than Video 1 in
# [-10, +10]; the caller remaps that signed score back to candidate-vs-baseline
# from the (hidden) randomized assignment. The loop stops when the candidate
# beats the baseline (remapped score > 0).
_REL_INSTRUCTION = (
    "You are an impartial physics-video comparison judge in a BLIND A/B test. "
    "You are shown TWO videos that both attempt the SAME scenario:\n"
    "\"{question}\"\n\n"
    "They are labeled only \"Video 1\" and \"Video 2\". You are NOT told how "
    "either was made — judge them purely on what you observe, with no assumptions "
    "about which is which.\n\n"
    "Output ONE signed integer `score` in [-10, +10] = how much BETTER Video 2 "
    "is than Video 1, considering BOTH of:\n"
    "  (1) Semantic adherence — which video better matches the scenario's stated "
    "facts (object COUNT, color, material, identity, setting, the described "
    "action/sequence)?\n"
    "  (2) Physical correctness — which video's motion is more physically "
    "plausible (gravity, inertia, collisions, contact, fluids/soft-body; no "
    "interpenetration / floating / teleport / broken chain-reaction order)?\n\n"
    "Scoring scale (Video 1 is the zero point):\n"
    "   0  = the two are essentially equal (no clear difference).\n"
    "  +1..+3 = Video 2 slightly better.  +4..+6 = clearly better.  +7..+10 = "
    "dramatically better / Video 2 is correct where Video 1 is wrong.\n"
    "  -1..-10 = Video 2 is WORSE than Video 1 by the same magnitudes.\n"
    "Be conservative: use 0 when there is no clear difference. Judge only from "
    "observable evidence; do not invent requirements.\n\n"
    "You are ONLY a judge: score the two videos and describe the differences. Do "
    "NOT recommend tools or next steps — that decision belongs to another agent.\n\n"
    "Reply with ONLY a JSON object, no markdown fence:\n"
    "{{\"score\": int (-10..10), "
    "\"sa_note\": \"one sentence: Video 2 vs Video 1 on scenario match\", "
    "\"pc_note\": \"one sentence: Video 2 vs Video 1 on physics\", "
    "\"issues\": [\"short, concrete strings — what the WORSE of the two videos "
    "still gets wrong\"], "
    "\"summary\": \"one sentence overall comparison\"}}"
)

# Condition pre-check: BEFORE spending a generation, judge whether the planner's
# proposed conditioning (a sim reference video / keyframes / reference images +
# the text prompt) is sound enough to steer the generator toward a video that
# beats the baseline. Shown the BASELINE clip (what to improve on) and every
# proposed condition; returns reasonable + reason + concrete suggestions.
_COND_INSTRUCTION = (
    "You are a pre-generation reviewer for a physics-video pipeline. The goal is "
    "to produce a video of this scenario:\n\"{question}\"\n\n"
    "A BASELINE VIDEO (made from the raw scenario text alone) is shown first — it "
    "is what we are trying to BEAT. Then you are shown the planner's PROPOSED "
    "CONDITIONING for the next generation: possibly a physics REFERENCE VIDEO "
    "(from a simulator), reference image(s) / keyframes, and the text prompt that "
    "will be sent to the video generator.\n\n"
    "Your job: decide whether this conditioning is SOUND enough to steer the "
    "generator toward a result that is MORE faithful to the scenario AND more "
    "physically correct than the baseline. Judge, using only observable evidence:\n"
    "  (1) If a reference video is proposed: it is an ABSTRACT PHYSICS SIM whose "
    "ONLY purpose is to convey MOTION. Judge it PURELY on whether the motion / "
    "physical process is correct (objects rest on surfaces, not floating / "
    "interpenetrating; correct object COUNT; right material behavior — clay dents "
    "and keeps the dent, sand collapses, a chain reaction fires in order). "
    "IGNORE the sim's colors, exact shapes, textures, lighting and overall "
    "un-photorealistic look — those are NOT what the final video will copy and "
    "must NEVER be a reason to reject. Only a wrong MOTION/COUNT makes a sim "
    "unsound.\n"
    "  (2) If reference images / keyframes are proposed: do they depict the right "
    "object/identity/layout for the scenario?\n"
    "  (3) The text prompt: is it concrete and describes a full real scene (not a "
    "bare physics-demo look), consistent with the MOTION/COUNT of the reference "
    "video. Do NOT flag the prompt for differing from the sim video's colors or "
    "shapes (the sim is abstract; the prompt supplies the real appearance).\n"
    "  *** The reference video and the reference images are DIFFERENT KINDS of "
    "input and are SUPPOSED to look different: the sim video is an abstract "
    "physics render that only carries MOTION, while the images/prompt carry the "
    "real APPEARANCE. A mismatch in color, material, texture or exact shape "
    "BETWEEN the sim video and the images/prompt is EXPECTED and is NEVER a "
    "problem — never reject for it. Only reject on a wrong MOTION, wrong COUNT, or "
    "images that depict the wrong object/layout. ***\n"
    "  (4) Overall: would THIS package plausibly beat the baseline? If the "
    "conditioning is empty / irrelevant / broken, or no better than what the "
    "baseline already had, it is NOT reasonable.\n\n"
    "Be pragmatic, not perfectionist: if the conditioning is basically good and "
    "would likely improve on the baseline, mark it reasonable even if minor "
    "wording could be polished. Only reject when there is a real, fixable problem "
    "that would waste the generation.\n\n"
    "Reply with ONLY a JSON object, no markdown fence:\n"
    "{{\"reasonable\": true | false, "
    "\"reason\": \"one or two sentences on why\", "
    "\"suggestions\": [\"short, concrete fixes if NOT reasonable (what to "
    "re-simulate / re-image / reword); empty list if reasonable\"]}}"
)

def _require_requests() -> None:
    if requests is None:
        raise RuntimeError(
            "The `requests` package is required. Install it with `pip install requests`."
        )


def _read_required(explicit: Optional[str], env: str, kwarg: str) -> str:
    val = (explicit or os.environ.get(env) or "").strip()
    if not val:
        raise RuntimeError(f"{env} is not set. Export it or pass {kwarg}=... to GeminiVerifierCore.")
    return val


class GeminiVerifierCore:
    """Gemini-backed video verifier.

    ``verify_relative(candidate, baseline, question)`` returns a blind-A/B verdict
    ``{score, conclusion, sa_note, pc_note, issues, summary, _order}``;
    ``judge_condition(...)`` returns ``{reasonable, reason, suggestions}``.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        timeout: int = 180,
        max_retries: int = 3,
    ) -> None:
        _require_requests()
        self.api_key = _read_required(api_key, "GEMINI_API_KEY", "api_key")
        self.base_url = (base_url or os.environ.get("GEMINI_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self.model = (model or os.environ.get("GEMINI_MODEL") or DEFAULT_MODEL).strip()
        self.timeout = int(timeout)
        self.max_retries = max(1, int(max_retries))

    # ---- HTTP ----
    def _post_generate(self, parts: list, instruction: str) -> str:
        url = f"{self.base_url}/v1beta/models/{self.model}:generateContent"
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        payload = {
            "contents": [
                {
                    "role": "user",
                    "parts": list(parts) + [{"text": instruction}],
                }
            ],
            "generationConfig": {"temperature": 0},
        }
        last_err: Optional[Exception] = None
        for attempt in range(self.max_retries):
            try:
                resp = requests.post(url, headers=headers, json=payload, timeout=self.timeout)
                if resp.status_code != 200:
                    raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:300]}")
                data = resp.json()
                return self._extract_text(data)
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                if attempt + 1 < self.max_retries:
                    time.sleep(min(10, 1 + 2 * attempt))
        raise RuntimeError(f"{url} failed after {self.max_retries} retries: {last_err}")

    @staticmethod
    def _extract_text(data: Dict[str, Any]) -> str:
        candidates = data.get("candidates") or []
        if not candidates:
            raise RuntimeError(f"no candidates in response: {str(data)[:200]}")
        parts = (candidates[0].get("content") or {}).get("parts") or []
        for part in reversed(parts):
            txt = part.get("text")
            if txt:
                return txt
        raise RuntimeError(f"no text part in response: {str(data)[:200]}")

    # ---- Parsing ----
    @staticmethod
    def _parse_json(text: str) -> Dict[str, Any]:
        t = text.strip()
        if t.startswith("```"):
            t = re.sub(r"^```[a-zA-Z]*\n?", "", t)
            t = re.sub(r"\n?```$", "", t).strip()
        try:
            return json.loads(t)
        except Exception:  # noqa: BLE001
            m = re.search(r"\{.*\}", t, re.DOTALL)
            if not m:
                raise RuntimeError(f"verifier did not return JSON: {text[:200]}")
            return json.loads(m.group(0))

    # ---- Relative (blind A/B) API ----
    def _video_part(self, label: str, path: str) -> list:
        size_mb = Path(path).stat().st_size / 1e6
        if size_mb > MAX_INLINE_MB:
            raise ValueError(
                f"{label} video is {size_mb:.1f} MB, over the {MAX_INLINE_MB} MB inline limit."
            )
        return [
            {"text": f"=== {label} ==="},
            {"inline_data": {"mime_type": "video/mp4",
                             "data": base64.b64encode(Path(path).read_bytes()).decode("ascii")}},
        ]

    def verify_relative(
        self,
        candidate_path: str,
        baseline_path: str,
        question: str,
        seed: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Blind A/B compare a CANDIDATE video against a fixed BASELINE.

        The two clips are shown to the judge in a RANDOMIZED order, labeled only
        "Video 1" / "Video 2" — the judge is never told which is the baseline, so
        it cannot favor "ours". The judge ONLY scores and describes the two clips;
        it does NOT recommend tools (that decision belongs to the planner). The
        score (how much better Video 2 is than Video 1, [-10,+10]) is remapped to
        candidate-vs-baseline:

            score >= REL_STOP_THRESHOLD  => candidate clearly beats the baseline
            => conclusion STOP.

        Returns ``{score, conclusion, sa_note, pc_note, issues, summary,
        _order}``. ``issues`` lists what the candidate still gets wrong (only when
        the candidate is the weaker clip); ``_order`` records the slot assignment.
        """
        for label, p in (("candidate", candidate_path), ("baseline", baseline_path)):
            if not p or not Path(p).is_file():
                raise FileNotFoundError(f"{label}: {p}")
        question = (question or "").strip()
        if not question:
            raise ValueError("question must be non-empty")

        # Randomize which clip is Video 1 vs Video 2 (blind). seed lets the caller
        # make it reproducible / vary per turn.
        rng = random.Random(seed)
        candidate_is_v2 = rng.random() < 0.5
        if candidate_is_v2:
            v1_path, v2_path = baseline_path, candidate_path  # V1=baseline, V2=candidate
        else:
            v1_path, v2_path = candidate_path, baseline_path  # V1=candidate, V2=baseline

        parts = self._video_part("Video 1", v1_path) + self._video_part("Video 2", v2_path)
        instruction = _REL_INSTRUCTION.format(question=question)
        text = self._post_generate(parts, instruction)
        obj = self._parse_json(text)

        try:
            raw = max(-10, min(10, int(round(float(obj.get("score", 0))))))
        except Exception:  # noqa: BLE001
            raw = 0
        # raw is "how much better V2 is than V1". Remap to candidate-vs-baseline:
        # if candidate was V2, score is +raw; if candidate was V1, flip the sign.
        score = raw if candidate_is_v2 else -raw

        conclusion = "STOP" if score >= REL_STOP_THRESHOLD else "CONTINUE"
        # The judge's `issues` describe the WORSE of the two clips. Surface them to
        # the planner only when the CANDIDATE is the weaker one (score < 0); if the
        # candidate already wins, its own faults aren't the point.
        issues = obj.get("issues") or []
        if not isinstance(issues, list):
            issues = [str(issues)]
        return {
            "score": score,
            "conclusion": conclusion,
            "sa_note": str(obj.get("sa_note", "")).strip(),
            "pc_note": str(obj.get("pc_note", "")).strip(),
            "issues": [str(x) for x in issues] if score < 0 else [],
            "summary": str(obj.get("summary", "")).strip(),
            "_order": {"video1": "baseline" if candidate_is_v2 else "candidate",
                       "video2": "candidate" if candidate_is_v2 else "baseline",
                       "raw_v2_minus_v1": raw},
        }

    # ---- Condition pre-check (before generation) ----
    def judge_condition(
        self,
        baseline_path: str,
        question: str,
        ref_video: Optional[str] = None,
        ref_images: Optional[list] = None,
        video_prompt: Optional[str] = None,
        history: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Judge whether the planner's proposed conditioning is sound BEFORE we
        spend a generation on it.

        Shows the judge the BASELINE clip (the thing to beat) and the proposed
        conditioning (sim ref video + reference images + the text prompt). Returns
        ``{"reasonable": bool, "reason": str, "suggestions": [str]}``.
        """
        if not baseline_path or not Path(baseline_path).is_file():
            raise FileNotFoundError(baseline_path)
        question = (question or "").strip()
        if not question:
            raise ValueError("question must be non-empty")

        parts: list = [{"text": "=== BASELINE VIDEO (the result to beat) ==="}]
        parts += self._video_part_data(baseline_path)
        has_cond = False
        if ref_video and Path(ref_video).is_file() and Path(ref_video).stat().st_size / 1e6 <= MAX_INLINE_MB:
            parts.append({"text": "=== PROPOSED CONDITION: physics REFERENCE VIDEO "
                                  "(the motion the generator will be told to follow) ==="})
            parts.append({"inline_data": {"mime_type": "video/mp4",
                          "data": base64.b64encode(Path(ref_video).read_bytes()).decode("ascii")}})
            has_cond = True
        for i, ip in enumerate(ref_images or []):
            if ip and Path(ip).is_file():
                mime = "image/png" if str(ip).lower().endswith(".png") else "image/jpeg"
                parts.append({"text": f"=== PROPOSED CONDITION: reference image {i + 1} ==="})
                parts.append({"inline_data": {"mime_type": mime,
                              "data": base64.b64encode(Path(ip).read_bytes()).decode("ascii")}})
                has_cond = True

        instruction = _COND_INSTRUCTION.format(question=question)
        instruction += ("\n\nPROPOSED TEXT PROMPT for the generator:\n\"" +
                        (video_prompt or "").strip() + "\"")
        if not has_cond:
            instruction += ("\n\nNOTE: no reference video or images were proposed — this is a "
                            "text-only condition. Judge whether the text prompt alone is "
                            "concrete and strong enough to plausibly beat the baseline.")
        if history and history.strip():
            instruction += ("\n\nHISTORY of earlier attempts (for context):\n" + history.strip())

        text = self._post_generate(parts, instruction)
        obj = self._parse_json(text)
        suggestions = obj.get("suggestions") or []
        if not isinstance(suggestions, list):
            suggestions = [str(suggestions)]
        return {
            "reasonable": bool(obj.get("reasonable", False)),
            "reason": str(obj.get("reason", "")).strip(),
            "suggestions": [str(x) for x in suggestions],
        }

    def _video_part_data(self, path: str) -> list:
        """Inline a single video as a one-element parts list (no label text)."""
        size_mb = Path(path).stat().st_size / 1e6
        if size_mb > MAX_INLINE_MB:
            raise ValueError(f"video is {size_mb:.1f} MB, over the {MAX_INLINE_MB} MB inline limit.")
        return [{"inline_data": {"mime_type": "video/mp4",
                                 "data": base64.b64encode(Path(path).read_bytes()).decode("ascii")}}]


def main() -> None:
    """CLI smoke check: blind A/B compare a candidate against a baseline.

    Usage: python core.py <candidate_video> <baseline_video> "<question>"
    Needs GEMINI_API_KEY (and optionally GEMINI_BASE_URL / GEMINI_MODEL) in env.
    """
    import sys

    if len(sys.argv) < 4:
        print('usage: python core.py <candidate_video> <baseline_video> "<question>"')
        raise SystemExit(2)
    candidate, baseline, question = sys.argv[1], sys.argv[2], sys.argv[3]
    result = GeminiVerifierCore().verify_relative(candidate, baseline, question)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

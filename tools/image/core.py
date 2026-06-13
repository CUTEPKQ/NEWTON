"""Self-contained image generate/edit core for OpenNewton tools.

Speaks an OpenAI-compatible image API. Model-agnostic: model, base url,
api-key, auth style and the provider's supported-size set are all
configuration (env vars or constructor kwargs).

Two modes, selected by whether reference images are supplied:

  - **generate** (text-to-image): ``POST {base_url}/images/generations`` with a
    JSON body ``{model, prompt, size, n}``.
  - **edit** (image-conditioned): ``POST {base_url}/images/edits`` as multipart
    form-data with one or more ``image[]`` files plus ``prompt``.

Both response shapes ``data[].b64_json`` and ``data[].url`` are handled. Each
image is written under the output dir (default ``./outputs/images``) and the
local path returned. Downstream key-frame / video generators consume those
paths.

This module has no dependency on the rest of OpenNewton, so it survives the
removal of the legacy ``.claude/skills`` tree.
"""

from __future__ import annotations

import base64
import io
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None  # type: ignore

try:
    from PIL import Image
except ImportError:  # pragma: no cover
    Image = None  # type: ignore


DEFAULT_OUTPUT_DIR = str(Path.cwd() / "outputs" / "images")
DEFAULT_RESOLUTION = (1280, 720)  # 720p, 16:9
SUPPORTED_SIZES_ANY = "any"


def _require_requests() -> None:
    if requests is None:
        raise RuntimeError(
            "The `requests` package is required. Install it with `pip install requests`."
        )


def _require_pillow() -> None:
    if Image is None:
        raise RuntimeError(
            "Pillow is required for resolution post-processing. "
            "Install it with `pip install pillow`."
        )


def _parse_resolution(value: Any) -> tuple[int, int]:
    if isinstance(value, (tuple, list)) and len(value) == 2:
        return int(value[0]), int(value[1])
    if isinstance(value, str) and "x" in value.lower():
        w, h = value.lower().split("x", 1)
        return int(w), int(h)
    raise ValueError(f"invalid resolution: {value!r} (expected (w, h) or 'WxH')")


def _parse_supported_sizes(value: Any) -> Any:
    if value is None:
        return SUPPORTED_SIZES_ANY
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("", SUPPORTED_SIZES_ANY):
            return SUPPORTED_SIZES_ANY
        return [_parse_resolution(part) for part in v.split(",") if part.strip()]
    if isinstance(value, (list, tuple)):
        return [_parse_resolution(part) for part in value]
    raise ValueError(f"invalid supported_sizes: {value!r}")


def _closest_size(target: tuple[int, int], allowed: List[tuple[int, int]]) -> tuple[int, int]:
    tw, th = target
    target_ar = tw / th
    return min(allowed, key=lambda s: abs((s[0] / s[1]) - target_ar))


def _read_required(explicit: Optional[str], env: str, kwarg: str) -> str:
    val = (explicit or os.environ.get(env) or "").strip()
    if not val:
        raise RuntimeError(f"{env} is not set. Export it or pass {kwarg}=... to ImageCore.")
    return val


class ImageCore:
    """OpenAI-compatible image generate/edit client.

    Methods return ``{prompt, local_path, mode, index}`` records, one per
    produced image.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        output_dir: Optional[str] = None,
        resolution: Optional[Any] = None,
        supported_sizes: Optional[Any] = None,
        auth_header: Optional[str] = None,
        timeout: int = 300,
        max_retries: int = 3,
        max_workers: int = 4,
    ) -> None:
        _require_requests()
        _require_pillow()
        self.api_key = _read_required(api_key, "IMG_CREATE_API_KEY", "api_key")
        self.base_url = _read_required(base_url, "IMG_CREATE_BASE_URL", "base_url").rstrip("/")
        self.model = _read_required(model, "IMG_CREATE_MODEL", "model")
        self.auth_header = (auth_header or os.environ.get("IMG_CREATE_AUTH_HEADER") or "api-key").strip().lower()
        # API protocol: "openai" (default, /images/generations|edits) or "gemini"
        # (a Gemini-compatible generateContent gateway returning image parts as
        # inline_data).
        self.api_kind = (os.environ.get("IMG_CREATE_API") or "openai").strip().lower()
        self.output_dir = Path(output_dir or os.environ.get("IMG_CREATE_OUTPUT_DIR") or DEFAULT_OUTPUT_DIR)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.resolution = _parse_resolution(
            resolution or os.environ.get("IMG_CREATE_RESOLUTION") or DEFAULT_RESOLUTION
        )
        self.supported_sizes = _parse_supported_sizes(
            supported_sizes if supported_sizes is not None else os.environ.get("IMG_CREATE_SUPPORTED_SIZES")
        )
        self.timeout = int(timeout)
        self.max_retries = max(1, int(max_retries))
        self._executor = ThreadPoolExecutor(max_workers=max(1, int(max_workers)))

    # ---- size strategy ----
    def _api_size_for(self, resolution: tuple[int, int]) -> str:
        if self.supported_sizes == SUPPORTED_SIZES_ANY:
            w, h = resolution
            return f"{w}x{h}"
        w, h = _closest_size(resolution, self.supported_sizes)
        return f"{w}x{h}"

    # ---- HTTP ----
    def _headers(self) -> Dict[str, str]:
        if self.auth_header == "bearer":
            return {"Authorization": f"Bearer {self.api_key}"}
        return {"api-key": self.api_key}

    def _post_generate(self, prompt: str, size: str, n: int) -> List[bytes]:
        url = f"{self.base_url}/images/generations"
        payload = {"model": self.model, "prompt": prompt, "size": size, "n": int(n)}
        headers = {**self._headers(), "Content-Type": "application/json"}
        return self._post_with_retry(url, lambda: requests.post(url, headers=headers, json=payload, timeout=self.timeout))

    def _post_edit(self, prompt: str, image_paths: Sequence[str], size: str, n: int) -> List[bytes]:
        url = f"{self.base_url}/images/edits"
        # Re-encode every reference to PNG in memory: the edit endpoint rejects
        # some source encodings (e.g. JPEG -> HTTP 400 "Invalid image file or
        # mode"), and PNG is the format it accepts reliably.
        files = []
        for i, p in enumerate(image_paths):
            if not p or not Path(p).exists():
                continue
            img = Image.open(p)
            if img.mode not in ("RGB", "RGBA"):
                img = img.convert("RGBA")
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            buf.seek(0)
            files.append(("image[]", (f"image_{i}.png", buf, "image/png")))
        if not files:
            raise RuntimeError("edit mode requires at least one existing image path")
        data = {"model": self.model, "prompt": prompt, "size": size, "n": str(int(n))}
        return self._post_with_retry(
            url,
            lambda: requests.post(url, headers=self._headers(), data=data, files=files, timeout=self.timeout),
        )

    def _post_with_retry(self, url: str, do_request) -> List[bytes]:
        last_err: Optional[Exception] = None
        for attempt in range(self.max_retries):
            try:
                resp = do_request()
                if resp.status_code != 200:
                    raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:300]}")
                data = resp.json()
                if isinstance(data, dict) and data.get("error"):
                    raise RuntimeError(f"API error: {data.get('error')}")
                items = data.get("data") or []
                raws: List[bytes] = []
                for it in items:
                    b64 = it.get("b64_json")
                    if b64:
                        raws.append(base64.b64decode(b64))
                    elif it.get("url"):
                        raws.append(self._download(it["url"]))
                if not raws:
                    raise RuntimeError(f"empty image payload: {str(data)[:200]}")
                return raws
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                if attempt + 1 < self.max_retries:
                    time.sleep(min(10, 1 + 2 * attempt))
        raise RuntimeError(f"{url} failed after {self.max_retries} retries: {last_err}")

    def _download(self, image_url: str) -> bytes:
        resp = requests.get(image_url, timeout=self.timeout)
        resp.raise_for_status()
        return resp.content

    # ---- Gemini generateContent backend (image-out via inline_data) ----
    def _gemini_post(self, parts: list) -> List[bytes]:
        """Call a Gemini generateContent gateway and return the image bytes from
        the response's inline_data parts. Used when IMG_CREATE_API=gemini."""
        url = f"{self.base_url}/v1beta/models/{self.model}:generateContent"
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        payload = {"contents": [{"role": "user", "parts": parts}]}
        last_err: Optional[Exception] = None
        for attempt in range(self.max_retries):
            try:
                resp = requests.post(url, headers=headers, json=payload, timeout=self.timeout)
                if resp.status_code != 200:
                    raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:300]}")
                data = resp.json()
                cands = data.get("candidates") or []
                raws: List[bytes] = []
                for c in cands:
                    for part in (c.get("content") or {}).get("parts") or []:
                        inline = part.get("inline_data") or part.get("inlineData")
                        if inline and inline.get("data"):
                            raws.append(base64.b64decode(inline["data"]))
                if not raws:
                    raise RuntimeError(f"no image part in response: {str(data)[:200]}")
                return raws
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                if attempt + 1 < self.max_retries:
                    time.sleep(min(10, 1 + 2 * attempt))
        raise RuntimeError(f"{url} failed after {self.max_retries} retries: {last_err}")

    def _gemini_generate(self, prompt: str) -> List[bytes]:
        return self._gemini_post([{"text": "Generate an image. " + prompt}])

    def _gemini_edit(self, prompt: str, image_paths: Sequence[str]) -> List[bytes]:
        parts: list = []
        for p in image_paths:
            if not p or not Path(p).exists():
                continue
            img = Image.open(p)
            if img.mode not in ("RGB", "RGBA"):
                img = img.convert("RGB")
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            parts.append({"inline_data": {"mime_type": "image/png",
                                          "data": base64.b64encode(buf.getvalue()).decode("ascii")}})
        if not parts:
            raise RuntimeError("edit mode requires at least one existing image path")
        parts.append({"text": "Edit the image(s) as follows, output the resulting image. " + prompt})
        return self._gemini_post(parts)

    # ---- Output ----
    def _write_image(self, raw: bytes, resolution: tuple[int, int]) -> str:
        target_w, target_h = resolution
        img = Image.open(io.BytesIO(raw))
        if img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGB")
        src_w, src_h = img.size
        scale = max(target_w / src_w, target_h / src_h)
        new_w, new_h = round(src_w * scale), round(src_h * scale)
        img = img.resize((new_w, new_h), Image.LANCZOS)
        left = (new_w - target_w) // 2
        top = (new_h - target_h) // 2
        img = img.crop((left, top, left + target_w, top + target_h))
        target = self.output_dir / f"{uuid.uuid4().hex[:24]}.png"
        img.save(target, format="PNG")
        return str(target)

    def _run_one(
        self,
        prompt: str,
        image_paths: Sequence[str],
        resolution: tuple[int, int],
        n: int,
        mode: str,
        index: int,
    ) -> List[Dict[str, Any]]:
        if self.api_kind == "gemini":
            # gateway returns its own image size; we letterbox/crop in _write_image.
            raws = self._gemini_edit(prompt, image_paths) if mode == "edit" else self._gemini_generate(prompt)
        else:
            api_size = self._api_size_for(resolution)
            if mode == "edit":
                raws = self._post_edit(prompt, image_paths, api_size, n)
            else:
                raws = self._post_generate(prompt, api_size, n)
        out: List[Dict[str, Any]] = []
        for raw in raws:
            out.append({
                "prompt": prompt,
                "local_path": self._write_image(raw, resolution),
                "mode": mode,
                "index": index,
            })
        return out

    # ---- Public API ----
    def generate_or_edit(
        self,
        prompt: str,
        image_paths: Optional[Sequence[str]] = None,
        resolution: Optional[Any] = None,
        n: int = 1,
    ) -> List[Dict[str, Any]]:
        """One image: text-to-image when ``image_paths`` is empty, else an edit."""
        if not prompt or not prompt.strip():
            raise ValueError("prompt must be non-empty")
        image_paths = list(image_paths or [])
        res = _parse_resolution(resolution) if resolution else self.resolution
        mode = "edit" if image_paths else "generate"
        return self._run_one(prompt.strip(), image_paths, res, max(1, int(n)), mode, 0)

    def keyframes(
        self,
        first_prompt: str,
        last_prompt: str,
        reference_images: Optional[Sequence[str]] = None,
        resolution: Optional[Any] = None,
    ) -> List[Dict[str, Any]]:
        """First + last key-frame chain.

        Frame 0 (first) is generated from ``first_prompt`` (or edits
        ``reference_images`` if given). Frame 1 (last) edits the first frame's
        output so the two share subject/scene/lighting and differ only in state.
        Returns two records with ``index`` 0 (first) and 1 (last).
        """
        if not first_prompt or not first_prompt.strip():
            raise ValueError("first_prompt must be non-empty")
        if not last_prompt or not last_prompt.strip():
            raise ValueError("last_prompt must be non-empty")
        res = _parse_resolution(resolution) if resolution else self.resolution
        seed_refs = list(reference_images or [])

        first_mode = "edit" if seed_refs else "generate"
        first = self._run_one(first_prompt.strip(), seed_refs, res, 1, first_mode, 0)
        if not first:
            raise RuntimeError("failed to produce the first key frame")
        first_path = first[0]["local_path"]

        last = self._run_one(last_prompt.strip(), [first_path], res, 1, "edit", 1)
        if not last:
            raise RuntimeError("failed to produce the last key frame")
        return [first[0], last[0]]

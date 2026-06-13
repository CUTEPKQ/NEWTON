"""Self-contained text-to-image search core for OpenNewton tools.

Speaks the public Serper.dev REST protocol (``POST {base_url}/images`` with a
JSON body ``{q, num}`` returning an ``images`` array). Provider-agnostic: the
base url, api key and download dir are configuration (env vars or constructor
kwargs), and any Serper-compatible gateway works as long as it returns the same
``images`` shape.

Each result image is downloaded to a local cache and the local path returned;
downstream steps (img_create as a reference, or the video generator) consume the
local paths — never the remote URLs. Each record also carries a small base64
``thumbnail`` data URL so a multimodal planner can SEE the candidates and pick
one (it then references the chosen ``local_path``). Adapted from GenEvolve's
ImageSearchTool.

This module has no dependency on the rest of OpenNewton.
"""

from __future__ import annotations

import base64
import hashlib
import io
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None  # type: ignore

try:
    from PIL import Image
except ImportError:  # pragma: no cover
    Image = None  # type: ignore


DEFAULT_SERPER_BASE_URL = "https://google.serper.dev"
DEFAULT_DOWNLOAD_DIR = str(Path.cwd() / "outputs" / "image_search")
DEFAULT_THUMBNAIL_PX = 256
_IMG_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif")
# Many image CDNs reject requests without a browser-like User-Agent.
_DOWNLOAD_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}


def _require_requests() -> None:
    if requests is None:
        raise RuntimeError(
            "The `requests` package is required. Install it with `pip install requests`."
        )


def _read_required(explicit: Optional[str], env: str, kwarg: str) -> str:
    val = (explicit or os.environ.get(env) or "").strip()
    if not val:
        raise RuntimeError(f"{env} is not set. Export it or pass {kwarg}=... to ImageSearchCore.")
    return val


class ImageSearchCore:
    """Serper-compatible image search with local download caching.

    ``search`` returns ``{title, url, page_url, local_path, thumbnail, index}``
    records, one per successfully downloaded image. ``thumbnail`` is a small
    base64 data URL so a multimodal planner can SEE each candidate and pick one.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        download_dir: Optional[str] = None,
        timeout: int = 30,
        download_timeout: int = 10,
        max_retries: int = 3,
        download_workers: int = 8,
        thumbnail_px: int = DEFAULT_THUMBNAIL_PX,
    ) -> None:
        _require_requests()
        self.api_key = _read_required(api_key, "SERPER_API_KEY", "api_key")
        self.base_url = (
            base_url or os.environ.get("SERPER_BASE_URL") or DEFAULT_SERPER_BASE_URL
        ).rstrip("/")
        self.download_dir = Path(
            download_dir or os.environ.get("IMAGE_SEARCH_OUTPUT_DIR") or DEFAULT_DOWNLOAD_DIR
        )
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.timeout = int(timeout)
        # Per-image download budget: fail fast on an unreachable/slow host rather
        # than stalling the whole search on one bad image.
        self.download_timeout = int(download_timeout)
        self.max_retries = max(1, int(max_retries))
        # Thumbnail edge in px for the base64 preview fed to the planner (0 = off).
        env_thumb = os.environ.get("IMAGE_SEARCH_THUMBNAIL_PX")
        self.thumbnail_px = int(env_thumb) if env_thumb else int(thumbnail_px)
        self._executor = ThreadPoolExecutor(max_workers=max(1, int(download_workers)))

    # ---- HTTP ----
    def _post_images(self, query: str, num: int) -> List[Dict[str, Any]]:
        url = f"{self.base_url}/images"
        headers = {"X-API-KEY": self.api_key, "Content-Type": "application/json"}
        payload = {"q": query, "num": int(num)}
        last_err: Optional[Exception] = None
        for attempt in range(self.max_retries):
            try:
                resp = requests.post(url, headers=headers, json=payload, timeout=self.timeout)
                resp.raise_for_status()
                data = resp.json()
                return data.get("images") or []
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                if attempt + 1 < self.max_retries:
                    time.sleep(min(8, 1 + 2 * attempt))
        raise RuntimeError(f"Serper /images failed after {self.max_retries} retries: {last_err}")

    # ---- Download ----
    def _local_path_for(self, image_url: str) -> Path:
        digest = hashlib.sha1(image_url.encode("utf-8")).hexdigest()[:24]
        suffix = ".jpg"
        try:
            ext = Path(urlparse(image_url).path).suffix.lower()
            if ext in _IMG_EXTS:
                suffix = ext
        except Exception:  # noqa: BLE001
            pass
        return self.download_dir / f"{digest}{suffix}"

    def _download(self, image_url: str) -> Optional[str]:
        if not image_url:
            return None
        target = self._local_path_for(image_url)
        if target.exists() and target.stat().st_size > 0:
            return str(target)
        try:
            resp = requests.get(image_url, timeout=self.download_timeout, stream=True, headers=_DOWNLOAD_HEADERS)
            resp.raise_for_status()
            tmp = target.with_suffix(target.suffix + ".part")
            with tmp.open("wb") as fh:
                for chunk in resp.iter_content(chunk_size=8192):
                    if chunk:
                        fh.write(chunk)
            tmp.replace(target)
            return str(target)
        except Exception:  # noqa: BLE001
            return None

    def _thumbnail_data_url(self, local_path: str) -> str:
        """Small base64 JPEG data URL of a downloaded image, for the planner to see.

        Returns "" if thumbnails are disabled or Pillow is unavailable.
        """
        if self.thumbnail_px <= 0 or Image is None:
            return ""
        try:
            img = Image.open(local_path)
            img.thumbnail((self.thumbnail_px, self.thumbnail_px), Image.LANCZOS)
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=80)
            b64 = base64.b64encode(buf.getvalue()).decode("ascii")
            return f"data:image/jpeg;base64,{b64}"
        except Exception:  # noqa: BLE001
            return ""

    # ---- Public API ----
    def search(self, query: str, top_k: int = 3) -> List[Dict[str, Any]]:
        """Search images for ``query``; download each and return local records.

        Returns ``{title, url, page_url, local_path, thumbnail, index}`` per
        image that downloaded successfully (entries that fail to download are
        dropped). ``thumbnail`` is a base64 data URL preview for the planner.
        """
        query = (query or "").strip()
        if not query:
            raise ValueError("query must be non-empty")
        top_k = max(1, int(top_k))

        raw = self._post_images(query, top_k)
        records: List[Dict[str, Any]] = []
        urls: List[str] = []
        for item in raw:
            image_url = (
                item.get("imageUrl")
                or item.get("image_url")
                or item.get("thumbnailUrl")
                or item.get("url")
                or ""
            )
            if not image_url:
                continue
            records.append({
                "title": item.get("title") or "image",
                "url": image_url,
                "page_url": item.get("link") or item.get("source") or "",
                "local_path": "",
            })
            urls.append(image_url)

        futures = [self._executor.submit(self._download, u) for u in urls]
        for rec, fut in zip(records, futures):
            try:
                rec["local_path"] = fut.result() or ""
            except Exception:  # noqa: BLE001
                rec["local_path"] = ""

        out = [r for r in records if r["local_path"]]
        for i, r in enumerate(out):
            r["index"] = i
            r["thumbnail"] = self._thumbnail_data_url(r["local_path"])
        return out

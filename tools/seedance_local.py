"""Seedance 2.0 video generation with LOCAL file inputs.

Thin wrapper over seedance_run.py that accepts local image/video paths and
inlines them as base64 data URLs (the video API takes a data URL in place of
a public URL for images, and accepts an inlined video too as long as the whole
request body stays under 64 MB). Use this when you have a freshly rendered
simulator reference clip on disk and no public URL to host it.

Examples
--------
  # reference-video -> video (e.g. feed a physics sim clip as motion reference)
  python seedance_local.py \
      --text "three bowling pins struck by a ball, scattering" \
      --ref-video /path/to/sim.mp4 \
      --token sk-xxx --duration 4 --resolution 720p

  # poll an existing task
  python seedance_local.py --id task-xxxx --token sk-xxx
"""

import argparse
import base64
import mimetypes
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import seedance_run as sr  # reuse submit/query/poll/build_content


def to_data_url(path: str) -> str:
    """Return a data: URL for a local file (or pass through an http(s) URL)."""
    if path.startswith("http://") or path.startswith("https://") or path.startswith("data:"):
        return path
    if not os.path.isfile(path):
        raise FileNotFoundError(path)
    mime, _ = mimetypes.guess_type(path)
    if mime is None:
        mime = "video/mp4" if path.lower().endswith((".mp4", ".mov")) else "application/octet-stream"
    with open(path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("ascii")
    return f"data:{mime};base64,{b64}"


def main():
    p = argparse.ArgumentParser(description="Seedance 2.0 with local file inputs (base64 inlined)")
    p.add_argument("--id", help="existing task id to poll instead of submitting")
    p.add_argument("--text", help="text prompt")
    p.add_argument("--first-frame", help="first frame image (local path or url)")
    p.add_argument("--last-frame", help="last frame image (local path or url)")
    p.add_argument("--ref-image", action="append", default=[], help="reference image (local/url, repeatable 1-9)")
    p.add_argument("--ref-video", action="append", default=[], help="reference video (local/url, repeatable <=3)")
    p.add_argument("--ref-audio", action="append", default=[], help="reference audio (local/url, repeatable <=3)")
    p.add_argument("--model", default=sr.DEFAULT_MODEL)
    p.add_argument("--fast", action="store_true")
    p.add_argument("--resolution", default="720p", choices=["480p", "720p", "1080p"])
    p.add_argument("--ratio", default="adaptive",
                   choices=["16:9", "4:3", "1:1", "3:4", "9:16", "21:9", "adaptive"])
    p.add_argument("--duration", type=int, default=5, help="seconds [4,15] or -1 for auto")
    p.add_argument("--no-audio", action="store_true")
    p.add_argument("--seed", type=int, default=-1)
    p.add_argument("--watermark", action="store_true")
    p.add_argument("--return-last-frame", action="store_true")
    p.add_argument("--token", help="API key (overrides SEEDANCE_API_KEY)")
    p.add_argument("--out-dir", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "seedance_videos"))
    p.add_argument("--interval", type=int, default=15)
    p.add_argument("--timeout", type=int, default=1800)
    args = p.parse_args()

    if args.token:
        sr.TOKEN = args.token

    if args.id:
        task_id = args.id
    else:
        content = sr.build_content(
            text=args.text,
            first_frame=to_data_url(args.first_frame) if args.first_frame else None,
            last_frame=to_data_url(args.last_frame) if args.last_frame else None,
            reference_images=[to_data_url(x) for x in args.ref_image],
            reference_videos=[to_data_url(x) for x in args.ref_video],
            reference_audios=[to_data_url(x) for x in args.ref_audio],
        )
        # report request size so we can catch the 64 MB body limit early
        approx = sum(len(c.get(k, {}).get("url", "")) for c in content
                     for k in ("image_url", "video_url", "audio_url"))
        print("request inline payload ~= %.1f MB" % (approx / 1e6))
        model = sr.FAST_MODEL if args.fast else args.model
        task_id = sr.submit(
            content, model=model, resolution=args.resolution, ratio=args.ratio,
            duration=args.duration, generate_audio=not args.no_audio, seed=args.seed,
            watermark=args.watermark, return_last_frame=args.return_last_frame,
        )

    sr.poll(task_id, args.out_dir, args.interval, args.timeout)


if __name__ == "__main__":
    main()

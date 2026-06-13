import argparse
import http.client
import json
import os
import time
import urllib.request

# API host and key come from the environment so the video backend can be swapped
# without code changes (set SEEDANCE_HOST / SEEDANCE_API_KEY in .env).
HOST = os.environ.get("SEEDANCE_HOST", "")
TOKEN = os.environ.get("SEEDANCE_API_KEY", "")
ID_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seedance_tasks.jsonl")
STANDARD_MODEL = "doubao-seedance-2-0-260128"
FAST_MODEL = "doubao-seedance-2-0-fast-260128"
DEFAULT_MODEL = FAST_MODEL


def _conn():
    if not HOST:
        raise RuntimeError("set SEEDANCE_HOST env var (the video provider hostname)")
    return http.client.HTTPSConnection(HOST)


def _headers():
    if not TOKEN:
        raise RuntimeError("set SEEDANCE_API_KEY env var or pass --token")
    return {
        "Accept": "application/json",
        "Authorization": "Bearer " + TOKEN,
        "Content-Type": "application/json",
    }


def build_content(text=None, first_frame=None, last_frame=None,
                  reference_images=None, reference_videos=None, reference_audios=None):
    content = []
    if text:
        content.append({"type": "text", "text": text})
    if first_frame:
        content.append({"type": "image_url", "image_url": {"url": first_frame}, "role": "first_frame"})
    if last_frame:
        content.append({"type": "image_url", "image_url": {"url": last_frame}, "role": "last_frame"})
    for url in (reference_images or []):
        content.append({"type": "image_url", "image_url": {"url": url}, "role": "reference_image"})
    for url in (reference_videos or []):
        content.append({"type": "video_url", "video_url": {"url": url}, "role": "reference_video"})
    for url in (reference_audios or []):
        content.append({"type": "audio_url", "audio_url": {"url": url}, "role": "reference_audio"})
    if not content:
        raise ValueError("content is empty: provide at least --text or an image/video")
    return content


def submit(content, model=DEFAULT_MODEL, resolution="720p", ratio="adaptive",
           duration=5, generate_audio=True, seed=-1, watermark=False, return_last_frame=False):
    conn = _conn()
    body = {
        "model": model,
        "content": content,
        "resolution": resolution,
        "ratio": ratio,
        "duration": duration,
        "generate_audio": generate_audio,
        "seed": seed,
        "watermark": watermark,
        "return_last_frame": return_last_frame,
    }
    conn.request("POST", "/v1/videos", json.dumps(body), _headers())
    data = json.loads(conn.getresponse().read().decode("utf-8"))
    conn.close()
    task_id = data.get("id")
    if not task_id:
        raise RuntimeError("create failed: %s" % data)
    with open(ID_FILE, "a") as f:
        f.write(json.dumps({"id": task_id, "content": content, "ts": time.time()}) + "\n")
    print("submitted: %s" % task_id)
    return task_id


def query(task_id):
    conn = _conn()
    conn.request("GET", "/v1/videos/" + task_id, "", _headers())
    data = json.loads(conn.getresponse().read().decode("utf-8"))
    conn.close()
    return data


def download(url, out_path):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req) as resp, open(out_path, "wb") as f:
        f.write(resp.read())
    print("saved: %s" % out_path)


def poll(task_id, out_dir, interval=15, timeout=1800):
    os.makedirs(out_dir, exist_ok=True)
    deadline = time.time() + timeout
    while time.time() < deadline:
        data = query(task_id)
        status = data.get("status", "")
        print("[%s] status=%s progress=%s" % (time.strftime("%H:%M:%S"), status, data.get("progress", "")))
        if status == "completed":
            url = data.get("video_url")
            if not url:
                raise RuntimeError("completed but no video_url: %s" % data)
            out_path = os.path.join(out_dir, task_id.replace(":", "_").replace("/", "_") + ".mp4")
            download(url, out_path)
            return out_path
        if status == "failed":
            raise RuntimeError("task failed: %s / %s" % (data.get("code"), data.get("message")))
        time.sleep(interval)
    raise TimeoutError("timed out after %ss for %s" % (timeout, task_id))


def main():
    p = argparse.ArgumentParser(description="Seedance 2.0 video generation")
    p.add_argument("--id", help="existing task id to poll instead of submitting")
    p.add_argument("--text", help="text prompt")
    p.add_argument("--first-frame", help="first frame image url (img2video)")
    p.add_argument("--last-frame", help="last frame image url (first+last frame)")
    p.add_argument("--ref-image", action="append", default=[], help="reference image url (repeatable, 1-9)")
    p.add_argument("--ref-video", action="append", default=[], help="reference video url (repeatable, <=3)")
    p.add_argument("--ref-audio", action="append", default=[], help="reference audio url (repeatable, <=3)")
    p.add_argument("--model", default=DEFAULT_MODEL, help="model name")
    p.add_argument("--fast", action="store_true", help="use fast model (no 1080p)")
    p.add_argument("--resolution", default="720p", choices=["480p", "720p", "1080p"])
    p.add_argument("--ratio", default="adaptive",
                   choices=["16:9", "4:3", "1:1", "3:4", "9:16", "21:9", "adaptive"])
    p.add_argument("--duration", type=int, default=5, help="seconds [4,15] or -1 for auto")
    p.add_argument("--no-audio", action="store_true", help="generate silent video")
    p.add_argument("--seed", type=int, default=-1)
    p.add_argument("--watermark", action="store_true")
    p.add_argument("--return-last-frame", action="store_true")
    p.add_argument("--token", help="API key (overrides SEEDANCE_API_KEY)")
    p.add_argument("--out-dir", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "seedance_videos"))
    p.add_argument("--interval", type=int, default=15)
    p.add_argument("--timeout", type=int, default=1800)
    args = p.parse_args()

    global TOKEN
    if args.token:
        TOKEN = args.token

    if args.id:
        task_id = args.id
    else:
        content = build_content(
            text=args.text, first_frame=args.first_frame, last_frame=args.last_frame,
            reference_images=args.ref_image, reference_videos=args.ref_video, reference_audios=args.ref_audio,
        )
        model = FAST_MODEL if args.fast else args.model
        task_id = submit(
            content, model=model, resolution=args.resolution, ratio=args.ratio,
            duration=args.duration, generate_audio=not args.no_audio, seed=args.seed,
            watermark=args.watermark, return_last_frame=args.return_last_frame,
        )

    poll(task_id, args.out_dir, args.interval, args.timeout)


if __name__ == "__main__":
    main()

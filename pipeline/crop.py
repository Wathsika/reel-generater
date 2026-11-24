"""Speaker-aware vertical crop: the useful half of ClipsAI, without the 4 GB.

ClipsAI (https://github.com/ClipsAI/clipsai) turns a 16:9 recording into 9:16 by
finding who is speaking (pyannote speaker diarization) and where their face is,
then moving the crop window onto them. For podcasts, interviews and talking-head
sources that is a real improvement over a blind centre crop.

It is not a drop-in dependency here, though:

  * it installs WhisperX + PyTorch + pyannote (several GB, minutes on every free
    runner) and Pyannote needs a HuggingFace access token,
  * its clip *finding* splits on speaker turns, which is exactly wrong for a
    solo streamer - our LLM picker already does that better,
  * speaker identification is pointless when there is one voice.

So this module keeps the reframe and drops the rest. Frames are sampled from
the clip, faces are found with YuNet (a 230 KB ONNX model, vendored in
assets/models, MIT licence - no torch, no token), and the 9:16 window is placed
over the faces instead of over the middle of the picture.

Optional and conservative by design: it is off unless a campaign sets
`crop_mode: face`, and it falls back to the centre crop whenever a face is not
big and stable enough to be the subject. A tiny webcam face in the corner of a
gameplay stream is correctly ignored - cropping to it would throw away the game.
"""
from __future__ import annotations

import pathlib
import subprocess

from . import config, state

MODEL = (pathlib.Path(__file__).resolve().parent.parent
         / "assets" / "models" / "face_detection_yunet_2023mar.onnx")

# A face narrower than this share of the frame is not the subject (it is a
# webcam overlay, a face inside the game, or a background extra).
MIN_FACE_WIDTH = 0.08
# Below this much movement away from the middle it is not worth changing.
MIN_SHIFT = 0.06
SAMPLES = 7
DETECT_WIDTH = 320          # frames are scaled down before detection: fast

_detector = None
_sizes: dict[str, tuple[int, int]] = {}
_warned = False


def available() -> bool:
    try:
        import cv2  # noqa: F401
        import numpy  # noqa: F401
    except Exception:
        return False
    return True


def _load_detector():
    """('yunet'|'haar', detector) or None, built once per process."""
    global _detector, _warned
    if _detector is not None:
        return _detector or None
    if not available():
        if not _warned:
            state.warn("render", "face detection needs opencv - using the centre crop",
                       hint="pip install -r requirements.txt")
            _warned = True
        _detector = False
        return None

    import cv2

    try:
        if MODEL.is_file() and hasattr(cv2, "FaceDetectorYN"):
            net = cv2.FaceDetectorYN.create(str(MODEL), "", (DETECT_WIDTH, DETECT_WIDTH),
                                            0.6, 0.3, 5000)
            _detector = ("yunet", net)
        else:
            # Older opencv builds ship the Haar cascades; use them if the ONNX
            # model or the FaceDetectorYN API is missing.
            xml = pathlib.Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml"
            cascade = cv2.CascadeClassifier(str(xml))
            if cascade.empty():
                raise RuntimeError("no face model available")
            _detector = ("haar", cascade)
    except Exception as exc:
        if not _warned:
            state.warn("render", "face detection unavailable - using the centre crop",
                       reason=str(exc)[:140])
            _warned = True
        _detector = False
    return _detector or None


def _size(path: str) -> tuple[int, int]:
    """Source size, cached: the planner samples 7 frames from one file."""
    key = str(path)
    hit = _sizes.get(key)
    if hit is not None:
        return hit
    from .render import probe_size

    got = probe_size(path)
    if got[0] and got[1]:
        _sizes[key] = got
    return got


def _frame(path: str, seconds: float, width: int, height: int):
    """One BGR frame, scaled to width x height, as a numpy array."""
    import numpy as np

    cmd = [
        config.ffmpeg_bin(), "-v", "error",
        "-ss", f"{max(0.0, float(seconds)):.3f}", "-i", str(path),
        "-frames:v", "1",
        "-vf", f"scale={width}:{height}",
        "-f", "rawvideo", "-pix_fmt", "bgr24",
        "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, timeout=120)
    need = width * height * 3
    if proc.returncode != 0 or len(proc.stdout) < need:
        return None
    return np.frombuffer(proc.stdout[:need], dtype="uint8").reshape((height, width, 3))


def _boxes(kind: str, net, frame) -> list[tuple[int, int, int, int]]:
    """Face boxes in pixels for one frame."""
    import cv2

    h, w = frame.shape[:2]
    if kind == "yunet":
        net.setInputSize((w, h))
        _, faces = net.detect(frame)
        if faces is None:
            return []
        return [(int(f[0]), int(f[1]), int(f[2]), int(f[3])) for f in faces]
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    found = net.detectMultiScale(gray, scaleFactor=1.12, minNeighbors=6,
                                 minSize=(max(18, w // 14), max(18, w // 14)))
    return [(int(x), int(y), int(bw), int(bh)) for x, y, bw, bh in found]


def faces_at(path: str, seconds: float,
             width: int = DETECT_WIDTH) -> list[tuple[float, float, float, float]]:
    """Faces in one frame, as normalised (x, y, w, h) in 0..1."""
    found = _load_detector()
    if not found:
        return []
    src_w, src_h = _size(str(path))
    if not src_w or not src_h:
        return []
    height = max(16, int(round(width * src_h / src_w / 2)) * 2)
    frame = _frame(str(path), seconds, width, height)
    if frame is None:
        return []
    try:
        boxes = _boxes(found[0], found[1], frame)
    except Exception:
        return []
    return [(x / width, y / height, w / width, h / height) for x, y, w, h in boxes]


def _sample_times(start: float, end: float, samples: int = SAMPLES) -> list[float]:
    span = max(0.1, float(end) - float(start))
    # start a little inside the clip: frame 0 is often a fade
    return [float(start) + span * (0.06 + 0.88 * i / max(1, samples - 1))
            for i in range(samples)]


def plan(path: str, start: float, end: float, src_w: int, src_h: int) -> dict:
    """Where the 9:16 window should sit for this clip.

    Returns {"x": int|None, "faces": n, "reason": str}. `x is None` means the
    centre crop is already right.
    """
    if not src_w or not src_h:
        return {"x": None, "faces": 0, "reason": "unknown source size"}
    crop_w = min(src_w, src_h * 9 / 16)
    if crop_w >= src_w - 2:
        return {"x": None, "faces": 0, "reason": "source is already vertical"}
    if not available():
        return {"x": None, "faces": 0, "reason": "opencv not installed"}

    found: list[tuple[float, float, float, float]] = []
    for t in _sample_times(start, end):
        found.extend(faces_at(str(path), t))

    big = [f for f in found if f[2] >= MIN_FACE_WIDTH]
    if not big:
        return {"x": None, "faces": len(found),
                "reason": "no face big enough to follow" if found else "no faces found"}

    centres = sorted(f[0] + f[2] / 2 for f in big)
    median = centres[len(centres) // 2]
    # Faces scattered across the whole frame cannot all be kept: stay centred.
    if centres[-1] - centres[0] > (crop_w / src_w) * 0.8:
        return {"x": None, "faces": len(big), "reason": "faces too far apart"}

    x = median * src_w - crop_w / 2
    x = max(0.0, min(x, src_w - crop_w))
    if abs(x - (src_w - crop_w) / 2) < crop_w * MIN_SHIFT:
        return {"x": None, "faces": len(big), "reason": "already centred on the face"}
    return {"x": int(round(x)), "faces": len(big),
            "reason": f"following {len(big)} face(s) at {median:.0%} of the frame"}


def main() -> int:
    """`python -m pipeline.crop <video>` - report what the detector sees."""
    import argparse

    ap = argparse.ArgumentParser(description="Face-aware crop planner")
    ap.add_argument("video")
    ap.add_argument("--start", type=float, default=0.0)
    ap.add_argument("--end", type=float, default=10.0)
    args = ap.parse_args()

    if not available():
        print("opencv is not installed - install requirements.txt")
        return 2
    w, h = _size(args.video)
    print(f"source {w}x{h}")
    for t in _sample_times(args.start, args.end):
        faces = faces_at(args.video, t)
        print(f"  t={t:7.2f}s  faces={len(faces)}  "
              + ", ".join(f"x={f[0]:.2f} w={f[2]:.2f}" for f in faces))
    print("plan:", plan(args.video, args.start, args.end, w, h))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

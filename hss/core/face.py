"""Host-side face check (OpenCV YuNet detector + SFace recognizer) on the LAPTOP WEBCAM.

HONEST LIMITS (also in docs/THREE_FACTOR_DESIGN.md):
  * Camera = "Laptop webcam (development/test camera)", opened with OpenCV (DirectShow first, then any backend).
  * The result is reported to the device as a plaintext line over USB serial: malware on this PC can forge it.
  * LIVENESS / ANTI-SPOOFING IS NOT IMPLEMENTED: a printed photo or a video may pass.
  * No raw photos are stored anywhere; only the 128-float embedding (encrypted by the caller with DPAPI).
Heavy imports (cv2, numpy) are lazy so the rest of the app and its tests do not need them.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

log = logging.getLogger("hss.face")

CAMERA_LABEL = "Laptop webcam (development/test camera)"
COSINE_THRESHOLD = 0.363          # SFace's published cosine threshold
EMBEDDING_DIM = 128
TEMPLATE_BYTES = EMBEDDING_DIM * 4
DETECTOR_MODEL = "face_detection_yunet_2023mar.onnx"
RECOGNIZER_MODEL = "face_recognition_sface_2021dec.onnx"
LIVENESS = "not implemented"


class FaceError(Exception):
    code = "FACE_ERROR"


class CameraMissing(FaceError):
    code = "CAMERA_MISSING"


class CameraBusy(FaceError):
    code = "CAMERA_BUSY"


class ModelMissing(FaceError):
    code = "MODEL_MISSING"


class CaptureFailed(FaceError):
    code = "CAPTURE_FAILED"


@dataclass
class Sample:
    embedding: "object"                 # numpy float32 (128,), L2-normalised
    score: float                        # detector confidence
    width: int                          # face box width in pixels
    sharpness: float                    # variance of the Laplacian on the face crop


@dataclass
class FaceResult:
    matched: bool
    best_score: float = 0.0
    reason: str = ""                    # match | no_match | no_face | camera_error | no_template | timeout
    frames: int = 0


class FrameSource(Protocol):
    label: str

    def read(self): ...
    def close(self) -> None: ...


class Embedder(Protocol):
    def sample(self, frame) -> "Sample | None": ...


# ---------------------------------------------------------------- camera
class Camera:
    """OpenCV capture. `open_camera()` tries DirectShow then the default backend on indices 0..3."""
    label = CAMERA_LABEL

    def __init__(self, cap, index: int):
        self._cap, self.index = cap, index

    def read(self):
        ok, frame = self._cap.read()
        if not ok or frame is None:
            raise CameraBusy("The camera stopped delivering frames (busy, unplugged or blocked).")
        return frame

    def close(self) -> None:
        try:
            self._cap.release()
        except Exception:
            pass


def open_camera(indices=(0, 1, 2, 3), backends=None) -> Camera:
    import cv2
    backends = backends if backends is not None else (getattr(cv2, "CAP_DSHOW", 700), getattr(cv2, "CAP_ANY", 0))
    opened_but_silent = False
    for idx in indices:
        for be in backends:
            cap = cv2.VideoCapture(idx, be)
            try:
                if not cap.isOpened():
                    cap.release()
                    continue
                for _ in range(5):                     # warm-up: first frames are often black/empty
                    ok, frame = cap.read()
                    if ok and frame is not None:
                        return Camera(cap, idx)
                    time.sleep(0.05)
                opened_but_silent = True
                cap.release()
            except Exception:
                cap.release()
    if opened_but_silent:
        raise CameraBusy("A camera was found but delivers no frames: another app is using it, or Windows camera privacy blocks it.")
    raise CameraMissing("No camera found. Plug in / enable the laptop webcam.")


# ---------------------------------------------------------------- embedder (OpenCV)
class OpenCvEmbedder:
    """YuNet detection + SFace embedding. Raises ModelMissing if the ONNX files are not bundled."""

    MIN_SCORE = 0.85
    MIN_WIDTH = 110
    MIN_SHARPNESS = 35.0

    def __init__(self, model_dir: Path | str | None = None):
        import cv2
        d = Path(model_dir) if model_dir else default_model_dir()
        det, rec = d / DETECTOR_MODEL, d / RECOGNIZER_MODEL
        if not det.is_file() or not rec.is_file():
            raise ModelMissing(f"face models not found in {d} (see models/README.md)")
        self._cv2 = cv2
        self._det = cv2.FaceDetectorYN.create(str(det), "", (320, 320), 0.8, 0.3, 5000)
        self._rec = cv2.FaceRecognizerSF.create(str(rec), "")

    def sample(self, frame) -> "Sample | None":
        import numpy as np
        cv2 = self._cv2
        h, w = frame.shape[:2]
        self._det.setInputSize((w, h))
        _, faces = self._det.detect(frame)
        if faces is None or len(faces) != 1:           # none, or several people in view: no sample
            return None
        f = faces[0]
        x, y, bw, bh = (int(v) for v in f[:4])
        if x < 0 or y < 0 or x + bw > w or y + bh > h:  # partially out of frame
            return None
        crop = frame[y:y + bh, x:x + bw]
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        sharp = float(cv2.Laplacian(gray, cv2.CV_64F).var())
        aligned = self._rec.alignCrop(frame, f)
        emb = np.asarray(self._rec.feature(aligned), dtype=np.float32).reshape(-1)
        n = float(np.linalg.norm(emb))
        if emb.shape[0] != EMBEDDING_DIM or n == 0:
            return None
        return Sample(emb / n, float(f[14]), bw, sharp)


def default_model_dir() -> Path:
    from ..firstrun import bundled_dir
    return bundled_dir() / "models"


# ---------------------------------------------------------------- logic (testable with fake camera/embedder)
def cosine(a, b) -> float:
    import numpy as np
    a, b = np.asarray(a, dtype=np.float32), np.asarray(b, dtype=np.float32)
    d = float(np.linalg.norm(a) * np.linalg.norm(b))
    return float(np.dot(a, b) / d) if d else 0.0


def good_quality(s: Sample, strict: bool = True) -> bool:
    return (s.score >= OpenCvEmbedder.MIN_SCORE and s.width >= (OpenCvEmbedder.MIN_WIDTH if strict else 90)
            and s.sharpness >= OpenCvEmbedder.MIN_SHARPNESS)


def template_to_bytes(embedding) -> bytes:
    import numpy as np
    arr = np.asarray(embedding, dtype=np.float32).reshape(-1)
    if arr.shape[0] != EMBEDDING_DIM:
        raise ValueError("bad embedding size")
    return arr.tobytes()


def template_from_bytes(blob: bytes):
    import numpy as np
    if len(blob) != TEMPLATE_BYTES:
        raise ValueError("bad face template size")
    return np.frombuffer(blob, dtype=np.float32).copy()


def capture_template(cam: FrameSource, emb: Embedder, frames: int = 5, timeout: float = 25.0,
                     progress: Callable[[str], None] | None = None, clock=time.monotonic, sleep=time.sleep) -> bytes:
    """Enrollment capture: `frames` good-quality samples of ONE face, averaged. Nothing but numbers is kept."""
    import numpy as np
    say = progress or (lambda _m: None)
    samples: list[Sample] = []
    end = clock() + timeout
    rejected = 0
    while len(samples) < frames and clock() < end:
        s = emb.sample(cam.read())
        if s is None:
            rejected += 1
            say("Look at the camera: exactly one face, fully in view.")
        elif not good_quality(s):
            rejected += 1
            say(f"Quality too low (face width {s.width}px, sharpness {s.sharpness:.0f}): move closer / improve light, hold still.")
        else:
            samples.append(s)
            say(f"Captured {len(samples)}/{frames}")
        sleep(0.15)
    if len(samples) < frames:
        raise CaptureFailed(f"Could not capture {frames} good frames in {timeout:.0f}s (rejected {rejected}).")
    mean = np.mean([s.embedding for s in samples], axis=0)
    mean = mean / np.linalg.norm(mean)
    if min(cosine(s.embedding, mean) for s in samples) < 0.8:
        raise CaptureFailed("The captured frames do not look like the same person; try again.")
    return template_to_bytes(mean)


def verify_face(cam: FrameSource, emb: Embedder, template: bytes, timeout: float = 12.0, threshold: float = COSINE_THRESHOLD,
                need: int = 2, clock=time.monotonic, sleep=time.sleep) -> FaceResult:
    """Match against ONE enrolled template: `need` consecutive good frames at or above the threshold."""
    ref = template_from_bytes(template)
    end, streak, best, seen = clock() + timeout, 0, 0.0, 0
    while clock() < end:
        s = emb.sample(cam.read())
        if s is not None and good_quality(s, strict=False):
            seen += 1
            score = cosine(s.embedding, ref)
            best = max(best, score)
            streak = streak + 1 if score >= threshold else 0
            if streak >= need:
                return FaceResult(True, best, "match", seen)
        else:
            streak = 0
        sleep(0.1)
    return FaceResult(False, best, "no_match" if seen else "no_face", seen)

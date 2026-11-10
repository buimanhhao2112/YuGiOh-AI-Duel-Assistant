"""Non-invasive screen capture, local card-template matching, and combat-log OCR.

This module reads pixels only. It does not inspect game memory or send game input. Vision
dependencies are optional so the web dashboard can still start on machines without OpenCV
or Tesseract installed.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable


ARCHETYPE_TERMS = (
    "Snake-Eye", "Tenpai", "Sangen", "Yubel", "Branded", "Vanquish Soul", "K9",
    "Fiendsmith", "Mitsurugi", "Labrynth", "Mathmech", "Runick", "Kashtira",
)


class VisionUnavailableError(RuntimeError):
    """Raised when a requested capture/matching dependency is missing."""


@dataclass(frozen=True, slots=True)
class VisionConfig:
    hand_region: tuple[float, float, float, float] = (0.10, 0.68, 0.80, 0.30)
    log_region: tuple[float, float, float, float] = (0.015, 0.07, 0.34, 0.48)
    hand_card_count: int = 5
    card_template_width: int = 82
    card_template_height: int = 116
    match_threshold: float = 0.50
    max_templates: int = 1200
    capture_interval_seconds: float = 0.75
    ocr_interval_seconds: float = 1.5


class VisionEngine:
    """Capture one screen frame, match hand-card templates, and OCR the combat log."""

    def __init__(
        self,
        *,
        template_dir: str | Path | None = None,
        config: VisionConfig | None = None,
        tesseract_cmd: str | None = None,
    ) -> None:
        root = Path(__file__).resolve().parent
        self.template_dir = Path(template_dir) if template_dir else root / "data" / "card_thumbnails"
        self.config = config or VisionConfig()
        self.tesseract_cmd = self._find_tesseract(tesseract_cmd)
        self._lock = threading.Lock()
        self._ocr_lock = threading.Lock()
        self._templates: dict[str, Any] | None = None
        self._template_matrix: Any | None = None
        self._template_names: list[str] = []
        self._template_stamp: int | None = None
        self._last_ocr_time = 0.0
        self._last_ocr_result: tuple[str, str | None] = (
            "", "Tesseract OCR has not run yet."
        )

    @staticmethod
    def _find_tesseract(explicit: str | None = None) -> str | None:
        """Find a configured or standard Windows Tesseract executable without failing startup."""
        candidates = [
            explicit,
            os.environ.get("YGO_TESSERACT_CMD"),
            r"C:\Program Files\Tesseract-OCR\tesseract.exe",
            r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
            shutil.which("tesseract"),
        ]
        for candidate in candidates:
            if candidate and Path(candidate).is_file():
                return str(Path(candidate))
        return shutil.which("tesseract")

    def status(self) -> dict[str, Any]:
        modules = {name: importlib.util.find_spec(name) is not None for name in ("mss", "cv2", "numpy", "pytesseract")}
        template_count = len(self._load_templates()) if modules["cv2"] else 0
        return {
            "available": modules["mss"] and modules["cv2"],
            "modules": modules,
            "tesseract_binary": self.tesseract_cmd or shutil.which("tesseract"),
            "templates": template_count,
            "template_dir": str(self.template_dir),
            "config": asdict(self.config),
        }

    def _load_dependencies(self) -> tuple[Any, Any, Any | None, Any | None]:
        missing = [name for name in ("mss", "cv2", "numpy") if importlib.util.find_spec(name) is None]
        if missing:
            raise VisionUnavailableError(
                "Missing optional vision packages: " + ", ".join(missing) + ". Install requirements-vision.txt."
            )
        import cv2
        import mss
        import numpy as np

        pytesseract = None
        if importlib.util.find_spec("pytesseract") is not None:
            import pytesseract as pytesseract_module

            pytesseract = pytesseract_module
            if self.tesseract_cmd:
                pytesseract.pytesseract.tesseract_cmd = self.tesseract_cmd
        return mss, cv2, np, pytesseract

    def _load_templates(self) -> dict[str, Any]:
        with self._lock:
            try:
                directory_stamp = self.template_dir.stat().st_mtime_ns
            except OSError:
                directory_stamp = None
            if self._templates is not None and directory_stamp == self._template_stamp:
                return self._templates
            if importlib.util.find_spec("cv2") is None:
                return {}
            import cv2
            import numpy as np

            width, height = self.config.card_template_width, self.config.card_template_height
            templates: dict[str, Any] = {}
            descriptors: list[Any] = []
            names: list[str] = []
            allowed = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
            if self.template_dir.exists():
                for path in sorted(self.template_dir.iterdir(), key=lambda item: item.name.casefold()):
                    if path.suffix.casefold() not in allowed:
                        continue
                    image = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
                    if image is None or image.size == 0:
                        continue
                    resized = cv2.resize(image, (width, height), interpolation=cv2.INTER_AREA)
                    small = cv2.resize(resized, (41, 58), interpolation=cv2.INTER_AREA)
                    vector = cv2.equalizeHist(small).astype(np.float32).reshape(-1)
                    vector -= float(vector.mean())
                    norm = float(np.linalg.norm(vector))
                    if norm:
                        vector /= norm
                    name = path.stem.replace("_", " ")
                    templates[name] = vector
                    names.append(name)
                    descriptors.append(vector)
                    if len(templates) >= self.config.max_templates:
                        break
            self._templates = templates
            self._template_names = names
            self._template_matrix = np.stack(descriptors) if descriptors else None
            self._template_stamp = directory_stamp
            return templates

    @staticmethod
    def _pixel_box(image: Any, region: tuple[float, float, float, float]) -> tuple[int, int, int, int]:
        height, width = image.shape[:2]
        x, y, box_width, box_height = region
        left = max(0, min(width - 1, round(x * width)))
        top = max(0, min(height - 1, round(y * height)))
        right = max(left + 1, min(width, round((x + box_width) * width)))
        bottom = max(top + 1, min(height, round((y + box_height) * height)))
        return left, top, right, bottom

    def _ocr(self, gray_log: Any, pytesseract: Any | None) -> tuple[str, str | None]:
        with self._ocr_lock:
            if pytesseract is None:
                return "", "pytesseract is not installed. OCR is disabled."
            if not (self.tesseract_cmd or shutil.which("tesseract")):
                return "", "Tesseract executable was not found. Install Tesseract or set YGO_TESSERACT_CMD."
            now = time.monotonic()
            if now - self._last_ocr_time < self.config.ocr_interval_seconds:
                return self._last_ocr_result
            try:
                text = pytesseract.image_to_string(gray_log, config="--psm 6", timeout=1.5).strip()
                self._last_ocr_result = (text, None)
            except Exception as exc:  # OCR errors must not take down the UI/service.
                self._last_ocr_result = ("", f"OCR failed: {type(exc).__name__}: {exc}")
            self._last_ocr_time = now
            return self._last_ocr_result

    def scan_image(self, image: Any, *, monitor: int = 1) -> dict[str, Any]:
        """Process an already captured BGR/RGB image; useful for fixtures and uploaded captures."""
        _mss, cv2, np, pytesseract = self._load_dependencies()
        if image is None or getattr(image, "size", 0) == 0:
            raise ValueError("Screenshot image is empty.")
        if len(image.shape) == 2:
            gray = image
        elif image.shape[2] == 4:
            gray = cv2.cvtColor(image, cv2.COLOR_BGRA2GRAY)
        else:
            gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        gray = np.ascontiguousarray(gray)
        templates = self._load_templates()
        hand_left, hand_top, hand_right, hand_bottom = self._pixel_box(gray, self.config.hand_region)
        hand_roi = gray[hand_top:hand_bottom, hand_left:hand_right]
        card_count = max(1, self.config.hand_card_count)
        card_height = hand_roi.shape[0]
        card_width = min(hand_roi.shape[1], max(1, round(card_height * 0.71)))
        placements = [0] if card_count == 1 else [round(i * (hand_roi.shape[1] - card_width) / (card_count - 1)) for i in range(card_count)]
        found: list[dict[str, Any]] = []
        width, height = self.config.card_template_width, self.config.card_template_height
        if templates and self._template_matrix is not None:
            for index, offset in enumerate(placements, start=1):
                crop = hand_roi[:, offset:offset + card_width]
                normalized = cv2.resize(crop, (width, height), interpolation=cv2.INTER_AREA)
                descriptor = cv2.resize(normalized, (41, 58), interpolation=cv2.INTER_AREA)
                vector = cv2.equalizeHist(descriptor).astype(np.float32).reshape(-1)
                vector -= float(vector.mean())
                norm = float(np.linalg.norm(vector))
                if norm:
                    vector /= norm
                scores = self._template_matrix @ vector
                best_index = int(np.argmax(scores))
                score = float(scores[best_index])
                name = self._template_names[best_index]
                found.append({
                    "slot": index,
                    "name": name if score >= self.config.match_threshold else None,
                    "score": round(score, 4),
                    "candidate": name,
                })

        log_left, log_top, log_right, log_bottom = self._pixel_box(gray, self.config.log_region)
        log_crop = gray[log_top:log_bottom, log_left:log_right]
        log_crop = cv2.resize(log_crop, None, fx=1.5, fy=1.5, interpolation=cv2.INTER_CUBIC)
        log_crop = cv2.threshold(log_crop, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]
        ocr_text, ocr_warning = self._ocr(log_crop, pytesseract)
        lowered = ocr_text.casefold()
        archetypes = [term for term in ARCHETYPE_TERMS if term.casefold() in lowered]
        return {
            "monitor": monitor,
            "hand": [entry["name"] for entry in found if entry["name"]],
            "matches": found,
            "ocr_text": ocr_text,
            "ocr_warning": ocr_warning,
            "opponent_archetypes": archetypes,
            "templates_loaded": len(templates),
            "template_warning": None if templates else f"No card images found in {self.template_dir}",
            "captured_at": time.time(),
        }

    def scan_screen(self, *, monitor: int = 1) -> dict[str, Any]:
        mss, _cv2, np, _pytesseract = self._load_dependencies()
        with mss.mss() as capture:
            if monitor < 1 or monitor >= len(capture.monitors):
                raise ValueError(f"Monitor index {monitor} is unavailable; found {len(capture.monitors) - 1} monitor(s).")
            frame = np.asarray(capture.grab(capture.monitors[monitor]))[:, :, :3]
        result = self.scan_image(frame, monitor=monitor)
        result["screen_size"] = {"width": int(frame.shape[1]), "height": int(frame.shape[0])}
        return result


def inspect_master_duel_process(process_name: str = "MasterDuel.exe") -> dict[str, Any]:
    """Return whether Master Duel exists and whether it (or this overlay) is foreground."""
    pids: list[int] = []
    try:
        import psutil

        folded_name = process_name.casefold()
        for process in psutil.process_iter(("pid", "name", "exe")):
            try:
                info = process.info
                name = str(info.get("name") or Path(str(info.get("exe") or "")).name)
                if name.casefold() == folded_name:
                    pids.append(int(info["pid"]))
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
    except ImportError:
        if os.name == "nt":
            try:
                result = subprocess.run(
                    ["tasklist", "/FI", f"IMAGENAME eq {process_name}", "/FO", "CSV", "/NH"],
                    capture_output=True,
                    text=True,
                    timeout=2,
                    check=False,
                )
                if process_name.casefold() in result.stdout.casefold():
                    pids.append(-1)
            except (OSError, subprocess.SubprocessError):
                pass

    foreground_pid: int | None = None
    if os.name == "nt":
        try:
            import ctypes

            window = ctypes.windll.user32.GetForegroundWindow()
            pid = ctypes.c_ulong()
            ctypes.windll.user32.GetWindowThreadProcessId(window, ctypes.byref(pid))
            foreground_pid = int(pid.value)
        except (AttributeError, OSError):
            foreground_pid = None

    current_pid = os.getpid()
    running = bool(pids)
    foreground = running and (
        foreground_pid in pids or foreground_pid == current_pid or foreground_pid is None
    )
    return {
        "running": running,
        "foreground": foreground,
        "process_name": process_name,
        "pids": pids,
        "foreground_pid": foreground_pid,
    }


class VisionWorker:
    """Opt-in 0.5s capture worker; callbacks run on the worker thread."""

    def __init__(
        self,
        engine: VisionEngine,
        *,
        monitor: int = 1,
        callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.engine = engine
        self.monitor = monitor
        self.callback = callback
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._latest: dict[str, Any] = {"status": "idle"}
        self._latest_lock = threading.Lock()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def latest(self) -> dict[str, Any]:
        with self._latest_lock:
            return dict(self._latest)

    def start(self) -> bool:
        if self.running:
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="ygo-vision", daemon=True)
        self._thread.start()
        return True

    def stop(self, timeout: float = 1.5) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def _publish(self, value: dict[str, Any]) -> None:
        with self._latest_lock:
            self._latest = value
        if self.callback:
            self.callback(value)

    def _run(self) -> None:
        while not self._stop.is_set():
            started = time.monotonic()
            process_state = inspect_master_duel_process()
            if not process_state["running"] or not process_state["foreground"]:
                state = "waiting_for_game" if not process_state["running"] else "waiting_for_foreground"
                self._publish({"status": state, "game": process_state})
                self._stop.wait(self.engine.config.capture_interval_seconds)
                continue
            try:
                self._publish({"status": "scanning", **self.engine.scan_screen(monitor=self.monitor)})
            except Exception as exc:
                self._publish({"status": "unavailable", "error": f"{type(exc).__name__}: {exc}"})
                return
            delay = max(0.0, self.engine.config.capture_interval_seconds - (time.monotonic() - started))
            self._stop.wait(delay)


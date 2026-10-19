"""Run Blender headless with one of the scripts in backend/blender_scripts."""

from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable

from .config import settings

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "blender_scripts"
_PROGRESS = re.compile(r"\[stash-progress\] (\d+)/(\d+)")


class BlenderUnavailable(RuntimeError):
    pass


class BlenderError(RuntimeError):
    def __init__(self, msg: str, log: str):
        super().__init__(msg)
        self.log = log


def available() -> bool:
    return bool(settings.blender and Path(settings.blender).exists())


def version() -> str | None:
    if not available():
        return None
    try:
        out = subprocess.run([settings.blender, "--version"], capture_output=True, text=True, timeout=60).stdout
        return out.splitlines()[0].strip() if out else None
    except Exception:
        return None


def run_script(
    script: str,
    args: list[str],
    blend_file: str | Path | None = None,
    timeout: int | None = None,
    on_progress: Callable[[float], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> str:
    if not available():
        raise BlenderUnavailable("Blender is not available on this server (set STASH_BLENDER)")
    cmd = [settings.blender, "-b"]
    if settings.blender_threads > 0:
        cmd += ["--threads", str(settings.blender_threads)]
    if blend_file:
        cmd.append(str(blend_file))
    cmd += ["--factory-startup", "-noaudio", "--python-exit-code", "1", "--python", str(SCRIPTS_DIR / script), "--", *args]
    env = {k: v for k, v in os.environ.items() if k != "OCIO"}
    nice = max(0, min(19, settings.blender_nice))
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
        env=env,
    )
    if nice and hasattr(os, "setpriority"):
        try:
            os.setpriority(os.PRIO_PROCESS, proc.pid, nice)
        except OSError:
            pass
    lines: list[str] = []
    # The timeout is an *inactivity* limit: big .blend files may legitimately take hours, but
    # the scripts print progress per asset, so silence for this long means Blender is stuck.
    limit = timeout or settings.blender_timeout
    last_output = [time.time()]
    timed_out = threading.Event()

    def watchdog():
        while proc.poll() is None:
            if time.time() - last_output[0] > limit:
                timed_out.set()
                proc.kill()
                return
            time.sleep(2)

    threading.Thread(target=watchdog, daemon=True).start()
    assert proc.stdout is not None
    for line in proc.stdout:
        last_output[0] = time.time()
        # Cycles is chatty; keep only the useful lines
        if not line.startswith("Fra:"):
            lines.append(line.rstrip())
            if len(lines) > 4000:
                del lines[:1000]
        m = _PROGRESS.search(line)
        if m and on_progress:
            done, total = int(m.group(1)), int(m.group(2))
            on_progress(done / total if total else 0)
        if should_cancel and should_cancel():
            proc.kill()
            raise BlenderError("cancelled", "\n".join(lines))
    rc = proc.wait()
    log = "\n".join(lines)
    if timed_out.is_set():
        raise BlenderError(f"Blender produced no output for {limit}s and was stopped", log)
    if rc != 0:
        tail = "\n".join(lines[-15:])
        raise BlenderError(f"Blender exited with code {rc}: {tail[-600:]}", log)
    return log


def convert(
    input_path: Path | None,
    output_path: Path,
    fmt: str,
    objects: list[str] | None = None,
    search_dirs: list[str] | None = None,
    pbr: dict | None = None,
    material_only: bool = False,
    append: list[str] | None = None,
    action: str | None = None,
    should_cancel=None,
) -> str:
    import json

    args = ["--output", str(output_path), "--format", fmt]
    blend = None
    if input_path is not None:
        args += ["--input", str(input_path)]
        if str(input_path).lower().endswith(".blend"):
            blend = input_path
    if objects:
        args += ["--objects", ",".join(objects)]
    for d in search_dirs or []:
        args += ["--search-dir", d]
    if pbr:
        args += ["--pbr", json.dumps(pbr)]
    if material_only:
        args.append("--material-only")
    for a in append or []:
        args += ["--append-blend", a]
    if action:
        args += ["--action", action]
    return run_script("convert.py", args, blend_file=blend, should_cancel=should_cancel)

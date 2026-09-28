from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path


class KaggleLaunchError(RuntimeError):
    pass


def configured() -> bool:
    return bool(os.getenv("KAGGLE_VIDEO_KERNEL_ID") and (os.getenv("KAGGLE_API_TOKEN") or os.getenv("KAGGLE_USERNAME")))


def launch_existing_kernel(run_id: str, day: str, pair_start: int) -> dict:
    """Create a new executable version of the configured Kaggle kernel.

    Kaggle's supported CLI execution path is `kernels push`; it runs the
    kernel after pushing a version. We first pull the exact current kernel so
    Railway never constructs or edits the video notebook itself.
    """
    kernel = os.getenv("KAGGLE_VIDEO_KERNEL_ID", "").strip()
    if not kernel:
        raise KaggleLaunchError("KAGGLE_VIDEO_KERNEL_ID is not configured")

    work = Path(tempfile.mkdtemp(prefix="kaggle-video-trigger-"))
    try:
        pull = ["kaggle", "kernels", "pull", kernel, "-p", str(work), "-m"]
        p = subprocess.run(pull, capture_output=True, text=True, timeout=180)
        if p.returncode != 0:
            raise KaggleLaunchError(f"Kaggle pull failed: {(p.stderr or p.stdout)[-1500:]}")

        metadata_path = work / "kernel-metadata.json"
        if not metadata_path.exists():
            raise KaggleLaunchError("Kaggle pull did not return kernel-metadata.json")

        # Do not silently change notebook code or metadata. The trigger is a
        # re-execution of the current kernel version, not a code deployment.
        meta = json.loads(metadata_path.read_text(encoding="utf-8"))
        if str(meta.get("id", "")).strip() != kernel:
            raise KaggleLaunchError("Pulled Kaggle metadata does not match configured kernel")

        # Make the run identity available to the notebook without changing
        # its source. A Kaggle notebook can read these only if it is designed
        # to do so; the durable Railway handshake remains authoritative.
        env = os.environ.copy()
        env.update({
            "KAGGLE_VIDEO_RUN_ID": run_id,
            "KAGGLE_VIDEO_DAY": day,
            "KAGGLE_VIDEO_PAIR_START": str(pair_start),
        })

        push = ["kaggle", "kernels", "push", "-p", str(work)]
        p = subprocess.run(push, capture_output=True, text=True, timeout=300, env=env)
        if p.returncode != 0:
            raise KaggleLaunchError(f"Kaggle push/run failed: {(p.stderr or p.stdout)[-2000:]}")

        return {"launched": True, "kernel": kernel, "run_id": run_id, "pair_start": pair_start,
                "stdout": p.stdout[-1000:]}
    finally:
        shutil.rmtree(work, ignore_errors=True)

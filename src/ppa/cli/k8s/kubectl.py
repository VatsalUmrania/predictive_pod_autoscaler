"""kubectl wrapper for file operations."""

import subprocess


def _run(cmd: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
    """Run kubectl command with timeout."""
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def cp(src: str, dest: str, retries: int = 3) -> None:
    """Copy file to/from pod. Format: namespace/pod:path

    Retries on failure to handle timing issues with pod readiness.
    """
    last_result: subprocess.CompletedProcess | None = None
    for attempt in range(retries):
        result = _run(["kubectl", "cp", src, dest])
        if result.returncode == 0:
            return
        last_result = result
        if attempt < retries - 1:
            import sys
            import time
            print(f" CP retry {attempt+1}/{retries}: {result.stderr.strip()}", file=sys.stderr)
            time.sleep(2)

    if last_result is not None:
        raise subprocess.CalledProcessError(
            last_result.returncode,
            last_result.args,
            last_result.stdout or "",
            last_result.stderr or "",
        )


def exec_cmd(pod_path: str, *args: str) -> subprocess.CompletedProcess:
    """Execute in pod. Format: namespace/pod_name or pod_name (assumes default namespace)"""
    if "/" in pod_path:
        namespace, pod_name = pod_path.split("/", 1)
        return _run(["kubectl", "exec", "-n", namespace, pod_name, "--"] + list(args))
    else:
        return _run(["kubectl", "exec", pod_path, "--"] + list(args))


def mkdir(pod_path: str, *dirs: str) -> None:
    """Create directories in pod. Format: namespace/pod"""
    for d in dirs:
        result = exec_cmd(
            pod_path, "python3", "-c", f"import os; os.makedirs('{d}', exist_ok=True)"
        )
        if result.returncode != 0:
            pass  # Silently ignore mkdir errors


def validate_cluster() -> bool:
    """Validate kubectl and cluster connection."""
    try:
        result = _run(["kubectl", "cluster-info"])
        return result.returncode == 0
    except Exception:
        return False

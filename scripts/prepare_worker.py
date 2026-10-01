"""Build a source-only Python Worker directory, excluding local user data."""

from pathlib import Path
import shutil


ROOT = Path(__file__).resolve().parents[1]
TARGET = (ROOT / ".worker-build").resolve()
SOURCE = TARGET / "src"


def main():
    if TARGET.parent != ROOT.resolve():
        raise RuntimeError("Worker build directory must stay inside the workspace")
    if TARGET.exists():
        shutil.rmtree(TARGET)
    SOURCE.mkdir(parents=True)
    for name in ("worker.py",):
        shutil.copy2(ROOT / name, SOURCE / name)
    for folder in ("app", "templates"):
        shutil.copytree(
            ROOT / folder, SOURCE / folder,
            ignore=shutil.ignore_patterns(
                "__pycache__", "*.pyc", "memory_sessions.py",
                "login_limiter.py", "rakushifu_client.py"),
        )
    shutil.copytree(ROOT / "static", TARGET / "public" / "static")
    for name in ("wrangler.toml", "pyproject.toml", "uv.lock", "pylock.toml"):
        shutil.copy2(ROOT / name, TARGET / name)
    print(TARGET)


if __name__ == "__main__":
    main()

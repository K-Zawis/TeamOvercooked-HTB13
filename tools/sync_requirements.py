"""Generate pip requirements from uv.lock, or check for dependency drift."""

import argparse
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
EXPORTS = {
    "requirements.txt": ["--no-dev"],
    "requirements-validation.txt": [],
}
HEADER = (
    "# Generated from pyproject.toml and uv.lock; do not edit directly.\n"
    "# Regenerate with: python tools/sync_requirements.py\n"
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if either export is missing or stale")
    args = parser.parse_args()
    exports = {}
    try:
        # --locked also rejects a pyproject.toml/uv.lock mismatch. Finish all
        # exports before updating files, so resolution failures leave them alone.
        for name, options in EXPORTS.items():
            result = subprocess.run(
                ["uv", "export", "--locked", "--no-hashes", "--no-header", *options],
                cwd=ROOT, capture_output=True, text=True, check=True,
            )
            exports[name] = HEADER + result.stdout
    except FileNotFoundError:
        print("Install uv to regenerate/check exports. Pip users can install the existing files without uv.",
              file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as exc:
        print(exc.stderr, file=sys.stderr, end="")
        return exc.returncode
    stale = []
    for name, content in exports.items():
        path = ROOT / name
        if args.check:
            if not path.exists() or path.read_text() != content:
                stale.append(name)
        else:
            path.write_text(content)
            print(f"Updated {name}")
    if stale:
        print(f"Out-of-date requirements: {', '.join(stale)}. "
              "Run python tools/sync_requirements.py.", file=sys.stderr)
        return 1
    if args.check:
        print("Requirements exports match pyproject.toml and uv.lock.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

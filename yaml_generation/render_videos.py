"""Render previously validated Manim part YAMLs (one output mp4 per part).

Requires Manim and working renderer Python file. The script does not install LaTeX.
"""
import argparse
import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def parse_parts(s):
    result = []
    for piece in s.split(","):
        if "-" in piece:
            left, right = map(int, piece.split("-", 1))
            result += list(range(left, right + 1))
        else:
            result.append(int(piece))
    return list(dict.fromkeys(result))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--renderer", type=Path, required=True)
    parser.add_argument("--scene", default="YAMLAnimation")
    parser.add_argument("--parts", type=parse_parts, default=parse_parts("3-13"))
    parser.add_argument("--input", type=Path, default=ROOT / "output")
    parser.add_argument("--quality", choices=("l", "m", "h", "k"), default="l")
    parser.add_argument("--output", type=Path, default=ROOT / "videos")
    parser.add_argument("--manim", default="manim")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    renderer = args.renderer.resolve()
    if not renderer.exists():
        parser.error(f"Renderer not found: {renderer}")
    for part in args.parts:
        source_yaml = (args.input / f"part_{part:02d}_animations.yaml").resolve()
        if not source_yaml.exists():
            raise SystemExit(f"Missing YAML: {source_yaml}")
        name = f"part_{part:02d}_animations"
        # --output_file customizes the Manim output name; --media_dir segregates each part.
        # Existing renderer's optional narration mux may still override final file destination.
        target_dir = args.output / name
        cmd = [args.manim, f"-q{args.quality}", "--media_dir", str(target_dir),
               "--output_file", name, str(renderer), args.scene, "--", str(source_yaml)]
        print(f"\n[Part {part}] Running: {subprocess.list2cmdline(cmd)}", flush=True)
        result = subprocess.run(cmd, cwd=str(renderer.parent), check=False)
        if result.returncode != 0:
            raise SystemExit(f"Manim failed at part {part} (exit {result.returncode}).")
        videos = list(target_dir.rglob(f"{name}.mp4"))
        if videos:
            chosen = max(videos, key=lambda x: x.stat().st_mtime)
            print(f"Part {part}: video at {chosen}")
        else:
            print(f"Part {part}: renderer exited successfully but video filename was not found. "
                  "Check narrator-mux output settings and Manim's media directory.")


if __name__ == "__main__":
    main()

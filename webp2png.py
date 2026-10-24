#!/usr/bin/env python3
"""
webp2png.py — batch-convert WebP images to lossless PNG.

Usage:
    python webp2png.py FOLDER [--remove] [--recursive] [--overwrite] [--apng] [-n] [-q]

Examples:
    python webp2png.py ./images --remove
    python webp2png.py ./images -r --remove --overwrite

Requires: pip install Pillow
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

try:
    from PIL import Image
except ImportError:
    sys.exit("error: Pillow is required -> pip install Pillow")

WEBP_SUFFIXES = {".webp"}


def find_webp(folder: Path, recursive: bool) -> list[Path]:
    it = folder.rglob("*") if recursive else folder.iterdir()
    return sorted(p for p in it if p.is_file() and p.suffix.lower() in WEBP_SUFFIXES)


def convert_one(src: Path, *, overwrite: bool, apng: bool) -> tuple[str, str]:
    """Convert one file. Returns (status, message) with status in ok/skipped/error."""
    dst = src.with_suffix(".png")

    if dst.exists() and not overwrite:
        return "skipped", "destination exists (use --overwrite)"

    # Write to a temp file in the same directory, then rename atomically.
    tmp = dst.with_name(dst.name + ".part")

    try:
        with Image.open(src) as im:
            animated = getattr(im, "n_frames", 1) > 1
            if animated and not apng:
                return "skipped", f"animated ({im.n_frames} frames), use --apng"

            save_kwargs = {"format": "PNG", "optimize": True}

            icc = im.info.get("icc_profile")
            if icc:
                save_kwargs["icc_profile"] = icc

            if animated:
                frames, durations = [], []
                for i in range(im.n_frames):
                    im.seek(i)
                    frames.append(im.convert("RGBA"))
                    durations.append(im.info.get("duration", 100))
                save_kwargs.update(
                    save_all=True,
                    append_images=frames[1:],
                    duration=durations,
                    loop=im.info.get("loop", 0),
                )
                frames[0].save(tmp, **save_kwargs)
            else:
                im.load()  # force a full decode before touching the disk
                im.save(tmp, **save_kwargs)

        if tmp.stat().st_size == 0:
            raise OSError("produced an empty file")

        os.replace(tmp, dst)  # atomic on the same filesystem
        return "ok", str(dst)

    except Exception as exc:
        tmp.unlink(missing_ok=True)
        return "error", f"{type(exc).__name__}: {exc}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Batch-convert WebP images to lossless PNG.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    ap.add_argument("folder", type=Path, help="folder containing .webp files")
    ap.add_argument("-r", "--recursive", action="store_true", help="descend into subfolders")
    ap.add_argument("--remove", action="store_true",
                    help="delete the .webp source after a successful conversion")
    ap.add_argument("--overwrite", action="store_true",
                    help="overwrite existing .png files")
    ap.add_argument("--apng", action="store_true",
                    help="save animated WebP as animated PNG (APNG) instead of skipping")
    ap.add_argument("-n", "--dry-run", action="store_true", help="list what would be done")
    ap.add_argument("-q", "--quiet", action="store_true", help="only print errors and the summary")
    args = ap.parse_args(argv)

    if not args.folder.is_dir():
        ap.error(f"not a directory: {args.folder}")

    files = find_webp(args.folder, args.recursive)
    if not files:
        print("No .webp files found.", file=sys.stderr)
        return 0

    counts = {"ok": 0, "skipped": 0, "error": 0}
    removed = 0

    for src in files:
        if args.dry_run:
            print(f"[dry-run] would convert {src} -> {src.with_suffix('.png')}")
            continue

        status, msg = convert_one(src, overwrite=args.overwrite, apng=args.apng)
        counts[status] += 1

        if not args.quiet or status == "error":
            print(f"[{status}] {src} -> {msg}" if status == "ok" else f"[{status}] {src}: {msg}")

        if status == "ok" and args.remove:
            try:
                src.unlink()
                removed += 1
                if not args.quiet:
                    print(f"[removed] {src}")
            except OSError as exc:
                print(f"[warn] could not remove {src}: {exc}", file=sys.stderr)

    if not args.dry_run:
        print(
            f"\nDone: {counts['ok']} converted, {counts['skipped']} skipped, "
            f"{counts['error']} failed, {removed} source(s) removed.",
            file=sys.stderr,
        )

    return 1 if counts["error"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
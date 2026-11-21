"""Recognize physics questions in job-owned PNG/JPEG image assets.

This module is independent of Streamlit.  It sends the original images to a
configured multimodal OpenRouter model and validates the resulting review
manifest without modifying or preprocessing the source files.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sys
from pathlib import Path
from typing import Any, Sequence

from openrouter_client import ImageInput, OpenRouterClient


MAX_IMAGES = 8
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_IMAGE_REQUEST_BYTES = 20 * 1024 * 1024
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
JPEG_SIGNATURE = b"\xff\xd8\xff"

RECOGNITION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["schema_version", "source_assets", "questions"],
    "properties": {
        "schema_version": {"type": "integer", "const": 1},
        "source_assets": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["asset_id", "path", "mime_type", "original_name"],
                "properties": {
                    "asset_id": {"type": "string"},
                    "path": {"type": "string"},
                    "mime_type": {"type": "string", "enum": ["image/jpeg", "image/png"]},
                    "original_name": {"type": "string"},
                },
            },
        },
        "questions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "problem", "source_assets", "notes"],
                "properties": {
                    "id": {"type": "string"},
                    "problem": {"type": "string"},
                    "source_assets": {"type": "array", "items": {"type": "string"}},
                    "source_regions": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["asset_id", "bbox_normalized"],
                            "properties": {
                                "asset_id": {"type": "string"},
                                "bbox_normalized": {
                                    "type": "array",
                                    "items": {"type": "number"},
                                    "minItems": 4,
                                    "maxItems": 4,
                                },
                            },
                        },
                    },
                    "notes": {"type": "string"},
                },
            },
        },
    },
}


def write_atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except (OSError, ValueError):
        return False


def validate_image_file(path: Path, mime_type: str) -> bytes:
    """Read and validate one job-owned image without modifying or copying it."""
    if mime_type not in ("image/jpeg", "image/png"):
        raise ValueError(f"{path}: unsupported MIME type {mime_type!r}")
    if not path.is_file():
        raise ValueError(f"Missing source image: {path}")
    if path.is_symlink():
        raise ValueError(f"Source image may not be a symbolic link: {path}")
    data = path.read_bytes()
    if not data:
        raise ValueError(f"Source image is empty: {path}")
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError(f"Source image exceeds {MAX_IMAGE_BYTES} bytes: {path}")
    if mime_type == "image/png" and not data.startswith(PNG_SIGNATURE):
        raise ValueError(f"{path}: invalid PNG signature")
    if mime_type == "image/jpeg" and not data.startswith(JPEG_SIGNATURE):
        raise ValueError(f"{path}: invalid JPEG signature")
    return data


def load_source_assets(job_root: Path, assets: Sequence[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[ImageInput]]:
    """Resolve and validate source_assets metadata and return API image inputs."""
    if not isinstance(assets, (list, tuple)) or not assets:
        raise ValueError("At least one source image asset is required")
    if len(assets) > MAX_IMAGES:
        raise ValueError(f"At most {MAX_IMAGES} source images are allowed")

    source_directory = (job_root / "source" / "images").resolve()
    if not source_directory.is_dir():
        raise ValueError(f"Source image directory does not exist: {source_directory}")

    manifests: list[dict[str, Any]] = []
    image_inputs: list[ImageInput] = []
    seen_ids: set[str] = set()
    total_bytes = 0

    for position, asset in enumerate(assets, 1):
        if not isinstance(asset, dict):
            raise ValueError(f"source_assets[{position - 1}] must be an object")
        for field in ("asset_id", "path", "mime_type", "original_name"):
            if not isinstance(asset.get(field), str) or not asset[field].strip():
                raise ValueError(f"source_assets[{position - 1}] has invalid {field}")

        asset_id = asset["asset_id"]
        if asset_id in seen_ids:
            raise ValueError(f"Duplicate source asset_id: {asset_id}")
        seen_ids.add(asset_id)

        relative = Path(asset["path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"Unsafe source asset path: {asset['path']}")
        path = (job_root / relative).resolve()
        if not _is_within(path, source_directory):
            raise ValueError(f"Source asset is outside {source_directory}: {asset['path']}")

        data = validate_image_file(path, asset["mime_type"])
        total_bytes += len(data)
        if total_bytes > MAX_IMAGE_REQUEST_BYTES:
            raise ValueError(f"Source images exceed {MAX_IMAGE_REQUEST_BYTES} byte request limit")

        manifests.append({
            "asset_id": asset_id,
            "path": relative.as_posix(),
            "mime_type": asset["mime_type"],
            "original_name": asset["original_name"],
        })
        image_inputs.append(ImageInput(
            filename=path.name,
            mime_type=asset["mime_type"],
            data=data,
        ))
    return manifests, image_inputs


def validate_recognition_manifest(
    result: Any,
    source_assets: Sequence[dict[str, Any]],
    job_root: Path,
) -> dict[str, Any]:
    """Strictly validate a model recognition result for the review UI."""
    if not isinstance(result, dict):
        raise ValueError("Recognition result must be a JSON object")
    if set(result) != {"schema_version", "source_assets", "questions"}:
        raise ValueError("Recognition result has missing or unsupported top-level fields")
    if (not isinstance(result["schema_version"], int)
            or isinstance(result["schema_version"], bool)
            or result["schema_version"] != 1):
        raise ValueError("Recognition result schema_version must be 1")

    # The model is required to report the exact job-owned asset inventory.
    result_assets = result["source_assets"]
    if not isinstance(result_assets, list):
        raise ValueError("source_assets must be an array")
    expected_by_id = {asset["asset_id"]: asset for asset in source_assets}
    result_ids: set[str] = set()
    for asset in result_assets:
        if not isinstance(asset, dict):
            raise ValueError("Each source_assets entry must be an object")
        if set(asset) != {"asset_id", "path", "mime_type", "original_name"}:
            raise ValueError("A source_assets entry has missing or unsupported fields")
        asset_id = asset["asset_id"]
        if not isinstance(asset_id, str) or not asset_id:
            raise ValueError("source_assets asset_id must be a non-empty string")
        if asset_id in result_ids:
            raise ValueError(f"Duplicate source asset_id: {asset_id}")
        result_ids.add(asset_id)
        if asset_id not in expected_by_id:
            raise ValueError(f"Recognition result references unknown asset_id {asset_id}")
        if asset != expected_by_id[asset_id]:
            raise ValueError(f"Recognition result changed source asset metadata for {asset_id}")

    if set(result_ids) != set(expected_by_id):
        raise ValueError("Recognition result must include all job source assets")

    questions = result["questions"]
    if not isinstance(questions, list) or not questions:
        raise ValueError("Recognition result must contain at least one question")

    seen_question_ids: set[str] = set()
    for index, question in enumerate(questions):
        if not isinstance(question, dict):
            raise ValueError(f"questions[{index}] must be an object")
        required = {"id", "problem", "source_assets", "notes"}
        if not required.issubset(question) or set(question) - required - {"source_regions"}:
            raise ValueError(f"questions[{index}] has missing or unsupported fields")
        question_id = question["id"]
        if not isinstance(question_id, str) or not question_id.strip():
            raise ValueError(f"questions[{index}] has an empty id")
        if question_id in seen_question_ids:
            raise ValueError(f"Duplicate question id: {question_id}")
        seen_question_ids.add(question_id)
        if not isinstance(question["problem"], str) or not question["problem"].strip():
            raise ValueError(f"Question {question_id} has empty problem text")
        if not isinstance(question["notes"], str):
            raise ValueError(f"Question {question_id} notes must be a string")
        referenced = question["source_assets"]
        if not isinstance(referenced, list) or not referenced:
            raise ValueError(f"Question {question_id} must reference at least one source asset")
        if len(set(referenced)) != len(referenced):
            raise ValueError(f"Question {question_id} repeats a source asset")
        for asset_id in referenced:
            if not isinstance(asset_id, str) or asset_id not in expected_by_id:
                raise ValueError(f"Question {question_id} references unknown asset_id {asset_id!r}")

        regions = question.get("source_regions", [])
        if not isinstance(regions, list):
            raise ValueError(f"Question {question_id} source_regions must be an array")
        for region in regions:
            if not isinstance(region, dict) or set(region) != {"asset_id", "bbox_normalized"}:
                raise ValueError(f"Question {question_id} has an invalid source region")
            if region["asset_id"] not in referenced:
                raise ValueError(f"Question {question_id} region references an unassociated asset")
            bbox = region["bbox_normalized"]
            if not isinstance(bbox, list) or len(bbox) != 4:
                raise ValueError(f"Question {question_id} region bbox must contain four numbers")
            if not all(isinstance(value, (int, float)) and not isinstance(value, bool)
                       and math.isfinite(value) for value in bbox):
                raise ValueError(f"Question {question_id} region bbox values must be finite numbers")
            x, y, width, height = bbox
            if x < 0 or y < 0 or width <= 0 or height <= 0 or x + width > 1 or y + height > 1:
                raise ValueError(f"Question {question_id} region bbox is outside normalized image bounds")

    # Re-check original files after model parsing; never permit a manifest-only result.
    for asset in source_assets:
        validate_image_file(job_root / asset["path"], asset["mime_type"])
    return result


async def extract_questions(
    job_root: Path,
    source_assets: Sequence[dict[str, Any]],
    *,
    client: OpenRouterClient,
    model: str,
) -> dict[str, Any]:
    """Recognize questions and return a validated manifest."""
    job_root = job_root.resolve()
    manifests, images = load_source_assets(job_root, list(source_assets))
    prompt = (
        "Identify all distinct physics questions shown in these original job images. "
        "Do not solve any question and do not provide answers or worked solutions. "
        "Preserve complete wording, subparts, multiple-choice options, units, and "
        "readable LaTeX or mathematical notation. Associate each question with every "
        "image containing it, and include optional normalized region boxes only when "
        "you can determine them accurately. Record uncertainty, missing context, and "
        "any dependence on diagrams in notes. If an image is unclear or a diagram "
        "cannot be represented by OCR text, say so in notes rather than inventing it."
    )
    result = await client.json(
        model=model,
        schema=RECOGNITION_SCHEMA,
        name="image_question_recognition",
        text=prompt,
        images=images,
    )
    return validate_recognition_manifest(result, manifests, job_root)


async def extract_and_write(
    job_root: Path,
    source_assets: Sequence[dict[str, Any]],
    destination: Path,
    *,
    client: OpenRouterClient,
    model: str,
) -> dict[str, Any]:
    manifest = await extract_questions(
        job_root, source_assets, client=client, model=model,
    )
    write_atomic(destination, manifest)
    return manifest


def read_asset_manifest(job_root: Path, assets_json: Path | None) -> list[dict[str, Any]]:
    if assets_json is not None:
        raw = json.loads(assets_json.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            assets = raw.get("source_assets")
        else:
            assets = raw
        if not isinstance(assets, list):
            raise ValueError("Asset manifest must be an array or contain a source_assets array")
        return assets

    status = json.loads((job_root / "status.json").read_text(encoding="utf-8"))
    assets = status.get("source_assets")
    if not isinstance(assets, list):
        raise ValueError("status.json does not contain a source_assets array")
    return assets


async def async_main(args: argparse.Namespace) -> int:
    if not os.getenv("OPENROUTER_API_KEY"):
        raise RuntimeError("OPENROUTER_API_KEY is not set")
    job_root = args.job_root.resolve()
    assets = read_asset_manifest(job_root, args.assets_json)
    destination = args.output.resolve()
    client = OpenRouterClient(
        api_key=os.environ["OPENROUTER_API_KEY"],
        timeout=args.timeout,
        format_mode=args.format_mode,
        max_tokens=args.max_tokens,
        max_retries=args.api_retries,
    )
    try:
        manifest = await extract_and_write(
            job_root,
            assets,
            destination,
            client=client,
            model=args.model,
        )
        print(
            f"Recognized {len(manifest['questions'])} questions "
            f"from {len(manifest['source_assets'])} images; wrote {destination}"
        )
        return 0
    finally:
        await client.close()


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job-root", required=True, type=Path, help="Filesystem-backed job directory")
    parser.add_argument("--assets-json", type=Path,
                        help="Asset manifest JSON; defaults to source_assets in status.json")
    parser.add_argument("--output", type=Path,
                        help="Recognition destination (default: <job-root>/input/recognized_questions.json)")
    parser.add_argument("--model",
                        default=os.getenv("OPENROUTER_VISION_MODEL",
                                          os.getenv("OPENROUTER_MODEL", "openai/gpt-4.1")),
                        help="OpenRouter multimodal/vision model")
    parser.add_argument("--format-mode", choices=["auto", "json_schema", "json_object", "prompt"],
                        default="auto")
    parser.add_argument("--max-tokens", type=int, default=12000)
    parser.add_argument("--timeout", type=float, default=600)
    parser.add_argument("--api-retries", type=int, default=3)
    args = parser.parse_args()
    if args.output is None:
        args.output = args.job_root / "input" / "recognized_questions.json"
    return args


def main() -> int:
    args = arguments()
    try:
        return asyncio.run(async_main(args))
    except (KeyboardInterrupt, Exception) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

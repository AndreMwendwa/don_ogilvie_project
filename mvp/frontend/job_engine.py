"""Filesystem-backed, single-host MVP job orchestration (not a distributed queue)."""
from __future__ import annotations
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import warnings
from io import BytesIO
from datetime import datetime, timezone
from uuid import uuid4

from PIL import Image, UnidentifiedImageError

JOB_ROOT = Path(os.environ.get('TNK_JOBS_DIR', '/jobs')).resolve()
SCRIPT_ROOT = Path(os.environ.get('TNK_SCRIPT_DIR', '/app/mvp/rendering')).resolve()
RENDERER = Path(os.environ.get('TNK_RENDERER', str(SCRIPT_ROOT / 'manim_multi_question_openai_mux_fixed.py'))).resolve()
REFERENCE = os.environ.get('TNK_REFERENCE', '')
PROMPT = os.environ.get('TNK_PROMPT', '')
LAYOUT_PROMPT = os.environ.get('TNK_LAYOUT_PROMPT', '')
SECTIONS = os.environ.get('TNK_SECTIONS', '')
MAX_UPLOAD_MB = int(os.environ.get('TNK_MAX_UPLOAD_MB', '80'))
MAX_IMAGE_FILES = int(os.environ.get('TNK_MAX_IMAGE_FILES', '10'))
MAX_IMAGE_MB = int(os.environ.get('TNK_MAX_IMAGE_MB', '20'))
MAX_IMAGE_TOTAL_MB = int(os.environ.get('TNK_MAX_IMAGE_TOTAL_MB', '80'))
MAX_IMAGE_PIXELS = int(os.environ.get('TNK_MAX_IMAGE_PIXELS', '50000000'))
PARTS_RE = re.compile(r'^[1-9]\d*(?:-[1-9]\d*)?(?:,[1-9]\d*(?:-[1-9]\d*)?)*$')
STATES = {'queued', 'running', 'completed', 'failed'}
IMAGE_SIGNATURES = {
    'image/png': (b'\x89PNG\r\n\x1a\n', 'png'),
    'image/jpeg': (b'\xff\xd8\xff', 'jpg'),
}


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def job_dir(jid: str) -> Path:
    if not re.fullmatch(r'[0-9a-f]{32}', jid):
        raise ValueError('Invalid job ID')
    return JOB_ROOT / jid


def atomic_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=path.parent, delete=False) as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        tmp = Path(f.name)
    tmp.replace(path)


def read_job(jid: str) -> dict:
    return json.loads((job_dir(jid) / 'status.json').read_text(encoding='utf-8'))


def list_jobs() -> list[dict]:
    JOB_ROOT.mkdir(parents=True, exist_ok=True)
    jobs = []
    for p in JOB_ROOT.iterdir():
        if p.is_dir() and re.fullmatch(r'[0-9a-f]{32}', p.name) and (p / 'status.json').is_file():
            try:
                jobs.append(read_job(p.name))
            except (ValueError, OSError, json.JSONDecodeError):
                continue
    return sorted(jobs, key=lambda j: j.get('created_at', ''), reverse=True)


def parse_parts(parts: str) -> list[int]:
    if not PARTS_RE.fullmatch(parts):
        raise ValueError('Enter part numbers such as 1, 2-4 or 1,3,5-7')
    values = set()
    for chunk in parts.split(','):
        if '-' in chunk:
            a, b = map(int, chunk.split('-'))
            if b < a or b - a > 100:
                raise ValueError('Invalid part range')
            values.update(range(a, b + 1))
        else:
            values.add(int(chunk))
    if len(values) > 100:
        raise ValueError('Too many parts')
    return sorted(values)


def create_job(filename: str, pdf_bytes: bytes, parts: str, model: str, quality: str, stages: str) -> dict:
    if not pdf_bytes.startswith(b'%PDF-'):
        raise ValueError('Selected file is not a valid PDF header')
    if len(pdf_bytes) > MAX_UPLOAD_MB * 1024 * 1024:
        raise ValueError(f'PDF exceeds {MAX_UPLOAD_MB} MB limit')
    selected_parts = parse_parts(parts)
    if not model or len(model) > 160 or not re.fullmatch(r'[\w./:-]+', model):
        raise ValueError('Invalid model identifier')
    if quality not in ('l', 'm', 'h', 'k'):
        raise ValueError('Invalid quality')
    if stages not in ('full', 'inventory', 'yaml'):
        raise ValueError('Invalid pipeline stage')
    active = [j for j in list_jobs() if j.get('status') in ('queued', 'running')]
    if len(active) >= int(os.environ.get('TNK_MAX_ACTIVE_JOBS', '1')):
        raise ValueError('The rendering worker is busy. Please submit after the current job finishes.')
    jid = uuid4().hex
    root = job_dir(jid)
    (root / 'source').mkdir(parents=True, exist_ok=False)
    (root / 'input').mkdir()
    (root / 'videos').mkdir()
    (root / 'logs').mkdir()
    (root / 'source' / 'workbook.pdf').write_bytes(pdf_bytes)
    data = dict(id=jid, filename=Path(filename).name[:160], parts=selected_parts, model=model,
                quality=quality, stages=stages, status='queued', stage='Waiting to start',
                created_at=now(), updated_at=now(), error=None, completed_parts=[],
                pid=None)
    atomic_json(root / 'status.json', data)
    # Separate process survives Streamlit reruns. The host/container must stay running.
    proc = subprocess.Popen([sys.executable, str(Path(__file__).with_name('runner.py')), jid],
                            cwd=str(Path(__file__).parent), stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True, close_fds=True)
    data['pid'] = proc.pid
    # Runner may already have updated status; avoid clobbering its work.
    current = read_job(jid)
    if current['status'] == 'queued':
        current['pid'] = proc.pid
        atomic_json(root / 'status.json', current)
    return read_job(jid)


def _validate_image_job_options(model: str, quality: str, stages: str) -> None:
    if not model or len(model) > 160 or not re.fullmatch(r'[\w./:-]+', model):
        raise ValueError('Invalid model identifier')
    if quality not in ('l', 'm', 'h', 'k'):
        raise ValueError('Invalid quality')
    if stages not in ('full', 'yaml'):
        raise ValueError('Invalid pipeline stage')


def _safe_display_name(name: str, fallback: str) -> str:
    basename = name.replace('\\', '/').rsplit('/', 1)[-1]
    sanitized = ''.join(char for char in basename if char.isprintable() and char not in '/\\')
    sanitized = sanitized.strip().strip('. ')
    return sanitized[:160] or fallback


def _validate_decoded_image(data: bytes, mime_type: str, index: int) -> None:
    expected_format = 'PNG' if mime_type == 'image/png' else 'JPEG'
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as image:
                image_format = image.format
                width, height = image.size
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError,
            Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ValueError(f'Image {index} is corrupt or cannot be decoded') from exc
    if image_format != expected_format:
        raise ValueError(f'Image {index} content does not match its MIME type')
    if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
        raise ValueError(f'Image {index} exceeds the supported image dimensions')
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as image:
                image.verify()
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError, EOFError,
            Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ValueError(f'Image {index} is corrupt or cannot be decoded') from exc


def _validated_image_records(images: list[dict]) -> list[dict]:
    if not isinstance(images, list) or not images:
        raise ValueError('Select at least one PNG or JPEG image')
    if len(images) > MAX_IMAGE_FILES:
        raise ValueError(f'Upload no more than {MAX_IMAGE_FILES} images')

    max_file_bytes = MAX_IMAGE_MB * 1024 * 1024
    max_total_bytes = MAX_IMAGE_TOTAL_MB * 1024 * 1024
    records = []
    total_bytes = 0
    for index, image in enumerate(images, start=1):
        if not isinstance(image, dict):
            raise ValueError(f'Image {index} must include a name, MIME type, and bytes')
        name = image.get('name')
        mime_type = image.get('mime_type')
        data = image.get('data')
        if not isinstance(name, str) or not isinstance(mime_type, str):
            raise ValueError(f'Image {index} must include a valid name and MIME type')
        if not isinstance(data, bytes) or not data:
            raise ValueError(f'Image {index} is empty or has invalid data')
        if mime_type not in IMAGE_SIGNATURES or not data.startswith(IMAGE_SIGNATURES[mime_type][0]):
            raise ValueError(f'Image {index} content does not match a supported PNG or JPEG format')
        if len(data) > max_file_bytes:
            raise ValueError(f'Image {index} exceeds the {MAX_IMAGE_MB} MB per-image limit')
        total_bytes += len(data)
        if total_bytes > max_total_bytes:
            raise ValueError(f'Images exceed the {MAX_IMAGE_TOTAL_MB} MB total limit')
        _validate_decoded_image(data, mime_type, index)

        extension = IMAGE_SIGNATURES[mime_type][1]
        records.append({
            'asset_id': f'img_{index:04d}',
            'path': f'source/images/{index:04d}.{extension}',
            'mime_type': mime_type,
            'original_name': _safe_display_name(name, f'image-{index:04d}.{extension}'),
            'data': data,
        })
    return records


def _ensure_image_job_capacity(jid: str) -> None:
    active = [job for job in list_jobs()
              if job.get('id') != jid and job.get('status') in ('queued', 'running')]
    if len(active) >= int(os.environ.get('TNK_MAX_ACTIVE_JOBS', '1')):
        raise ValueError('The rendering worker is busy. Please submit after the current job finishes.')


def launch_image_phase(jid: str, phase: str) -> dict:
    if phase not in ('extract', 'generate'):
        raise ValueError('Invalid image job phase')
    info = read_job(jid)
    if info.get('input_kind') != 'image':
        raise ValueError('Only image jobs support extraction and generation phases')
    _ensure_image_job_capacity(jid)

    if phase == 'extract':
        if info.get('status') != 'queued' or info.get('review_status') != 'pending':
            raise ValueError('Image extraction can only start for a queued image job')
    else:
        if info.get('review_status') != 'approved' or info.get('status') != 'awaiting_review':
            raise ValueError('Approve the reviewed questions before starting generation')
        set_status(jid, status='queued', stage='Queued for generation')

    command = [sys.executable, str(Path(__file__).with_name('runner.py')), jid, phase]
    try:
        proc = subprocess.Popen(command, cwd=str(Path(__file__).parent),
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, start_new_session=True,
                                close_fds=True)
    except OSError as exc:
        if phase == 'generate':
            set_status(jid, status='awaiting_review', stage='Questions approved; generation not started')
        else:
            set_status(jid, status='failed', stage='Could not start image extraction', error=str(exc))
        raise

    current = read_job(jid)
    if current.get('status') == 'queued':
        current['pid'] = proc.pid
        atomic_json(job_dir(jid) / 'status.json', current)
    return read_job(jid)


def create_image_job(images: list[dict], model: str, quality: str, stages: str) -> dict:
    records = _validated_image_records(images)
    _validate_image_job_options(model, quality, stages)
    _ensure_image_job_capacity('')

    jid = uuid4().hex
    root = job_dir(jid)
    images_dir = root / 'source' / 'images'
    images_dir.mkdir(parents=True, exist_ok=False)
    (root / 'input').mkdir()
    (root / 'videos').mkdir()
    (root / 'logs').mkdir()
    source_assets = []
    for record in records:
        (root / record['path']).write_bytes(record['data'])
        source_assets.append({key: record[key] for key in
                              ('asset_id', 'path', 'mime_type', 'original_name')})

    data = dict(id=jid, filename=source_assets[0]['original_name'], parts=[],
                model=model, quality=quality, stages=stages, status='queued',
                stage='Waiting to start', created_at=now(), updated_at=now(),
                error=None, completed_parts=[], pid=None, input_kind='image',
                source_assets=source_assets, review_status='pending')
    atomic_json(root / 'status.json', data)
    return launch_image_phase(jid, 'extract')


def _validate_reviewed_manifest(jid: str, manifest: dict, info: dict) -> dict:
    if (not isinstance(manifest, dict) or type(manifest.get('schema_version')) is not int
            or manifest['schema_version'] != 1):
        raise ValueError('Reviewed questions must use schema_version 1')
    assets = manifest.get('source_assets')
    questions = manifest.get('questions')
    if not isinstance(assets, list) or not isinstance(questions, list):
        raise ValueError('Reviewed questions must include source_assets and questions arrays')
    if not questions:
        raise ValueError('Approve at least one question')

    trusted_assets = info.get('source_assets')
    if not isinstance(trusted_assets, list) or assets != trusted_assets:
        raise ValueError('Reviewed question source assets do not match this job')
    asset_ids = set()
    job_root = job_dir(jid).resolve()
    source_root = (job_root / 'source').resolve()
    images_root = (source_root / 'images').resolve()
    try:
        source_root.relative_to(job_root)
        images_root.relative_to(source_root)
    except ValueError:
        raise ValueError('Image source directory escapes the job directory') from None
    for asset in trusted_assets:
        if not isinstance(asset, dict):
            raise ValueError('Job source asset metadata is invalid')
        asset_id = asset.get('asset_id')
        relative_path = asset.get('path')
        mime_type = asset.get('mime_type')
        if (not isinstance(asset_id, str) or not asset_id or asset_id in asset_ids
                or not isinstance(relative_path, str)
                or not re.fullmatch(r'source/images/\d{4}\.(?:png|jpg)', relative_path)):
            raise ValueError('Job source asset metadata is invalid')
        asset_ids.add(asset_id)
        if not isinstance(mime_type, str) or mime_type not in IMAGE_SIGNATURES:
            raise ValueError('Job source asset metadata is invalid')
        expected_extension = IMAGE_SIGNATURES[mime_type][1]
        if not expected_extension or not relative_path.endswith('.' + expected_extension):
            raise ValueError('Job source asset metadata is invalid')
        candidate = (job_root / relative_path).resolve()
        try:
            candidate.relative_to(images_root)
        except ValueError:
            raise ValueError('A source asset path escapes the image directory') from None
        if not candidate.is_file():
            raise ValueError(f'Missing source image: {relative_path}')

    result_questions = []
    question_ids = set()
    for index, question in enumerate(questions, start=1):
        if not isinstance(question, dict):
            raise ValueError(f'Question {index} must be an object')
        question_id = question.get('id')
        problem = question.get('problem')
        source_refs = question.get('source_assets')
        if not isinstance(question_id, str) or not question_id.strip():
            raise ValueError(f'Question {index} has an empty or duplicate ID')
        question_id = question_id.strip()
        if question_id in question_ids:
            raise ValueError(f'Question {index} has an empty or duplicate ID')
        if not isinstance(problem, str) or not problem.strip():
            raise ValueError(f'Question {question_id} must have non-empty problem text')
        if not isinstance(source_refs, list) or any(
                not isinstance(asset_id, str) or asset_id not in asset_ids
                for asset_id in source_refs):
            raise ValueError(f'Question {question_id} references an unknown source asset')
        question_ids.add(question_id)

        normalized_question = {
            'id': question_id,
            'problem': problem,
            'source_assets': source_refs,
        }
        regions = question.get('source_regions', [])
        if not isinstance(regions, list):
            raise ValueError(f'Question {question_id} source_regions must be an array')
        normalized_regions = []
        for region in regions:
            if not isinstance(region, dict) or region.get('asset_id') not in source_refs:
                raise ValueError(f'Question {question_id} has an invalid source region')
            bbox = region.get('bbox_normalized')
            if (not isinstance(bbox, list) or len(bbox) != 4
                    or any(isinstance(value, bool) or not isinstance(value, (int, float))
                           or (isinstance(value, float) and not math.isfinite(value))
                           for value in bbox)):
                raise ValueError(f'Question {question_id} has invalid region coordinates')
            x, y, width, height = bbox
            if (x < 0 or y < 0 or width <= 0 or height <= 0
                    or x + width > 1 or y + height > 1):
                raise ValueError(f'Question {question_id} has invalid region coordinates')
            normalized_regions.append({'asset_id': region['asset_id'],
                                       'bbox_normalized': bbox})
        if 'source_regions' in question:
            normalized_question['source_regions'] = normalized_regions
        notes = question.get('notes', '')
        if not isinstance(notes, str):
            raise ValueError(f'Question {question_id} notes must be text')
        normalized_question['notes'] = notes
        result_questions.append(normalized_question)

    return {'schema_version': 1, 'source_assets': trusted_assets, 'questions': result_questions}


def save_reviewed_questions(jid: str, manifest: dict) -> dict:
    info = read_job(jid)
    if info.get('input_kind') != 'image':
        raise ValueError('Reviewed questions can only be saved for image jobs')
    if info.get('status') != 'awaiting_review' or info.get('review_status') != 'pending':
        raise ValueError('This image job is not awaiting question review')
    validated = _validate_reviewed_manifest(jid, manifest, info)
    atomic_json(job_dir(jid) / 'input' / 'reviewed_questions.json', validated)
    approved_at = now()
    with (job_dir(jid) / 'logs' / 'pipeline.log').open('a', encoding='utf-8') as log:
        log.write(f'\nReview approved at {approved_at}; {len(validated["questions"])} questions.\n')
    set_status(jid, review_status='approved', review_approved_at=approved_at,
               stage='Questions approved; ready to generate')
    return read_job(jid)


def tail_log(jid: str, max_bytes: int = 12000) -> str:
    path = job_dir(jid) / 'logs' / 'pipeline.log'
    if not path.is_file():
        return 'No logs available yet.'
    with path.open('rb') as f:
        f.seek(0, 2)
        f.seek(max(0, f.tell() - max_bytes))
        return f.read().decode('utf-8', errors='replace')


def videos_for(jid: str) -> list[Path]:
    root = job_dir(jid) / 'videos'
    return sorted(root.rglob('*.mp4')) if root.is_dir() else []


def set_status(jid: str, **changes):
    path = job_dir(jid) / 'status.json'
    data = read_job(jid)
    data.update(changes, updated_at=now())
    atomic_json(path, data)

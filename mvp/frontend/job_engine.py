"""Filesystem-backed, single-host MVP job orchestration (not a distributed queue)."""
from __future__ import annotations
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from uuid import uuid4

JOB_ROOT = Path(os.environ.get('TNK_JOBS_DIR', '/jobs')).resolve()
SCRIPT_ROOT = Path(os.environ.get('TNK_SCRIPT_DIR', '/app/mvp/rendering')).resolve()
RENDERER = Path(os.environ.get('TNK_RENDERER', str(SCRIPT_ROOT / 'manim_multi_question_openai_mux_fixed.py'))).resolve()
REFERENCE = os.environ.get('TNK_REFERENCE', '')
PROMPT = os.environ.get('TNK_PROMPT', '')
LAYOUT_PROMPT = os.environ.get('TNK_LAYOUT_PROMPT', '')
SECTIONS = os.environ.get('TNK_SECTIONS', '')
MAX_UPLOAD_MB = int(os.environ.get('TNK_MAX_UPLOAD_MB', '80'))
PARTS_RE = re.compile(r'^[1-9]\d*(?:-[1-9]\d*)?(?:,[1-9]\d*(?:-[1-9]\d*)?)*$')
STATES = {'queued', 'running', 'completed', 'failed'}


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

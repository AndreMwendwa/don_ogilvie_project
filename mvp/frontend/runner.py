"""Execute the existing generator and renderer in a durable job folder.

Started by the Streamlit UI as a detached child. Not a production queue.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import traceback
from job_engine import (job_dir, read_job, set_status, SCRIPT_ROOT, RENDERER,
                        REFERENCE, PROMPT, LAYOUT_PROMPT, SECTIONS)


def command(cmd: list[str], logfile, stage: str, jid: str):
    set_status(jid, status='running', stage=stage)
    logfile.write(f'\n=== {stage} ===\n$ {" ".join(cmd)}\n')
    logfile.flush()
    result = subprocess.run(cmd, stdout=logfile, stderr=subprocess.STDOUT,
                            cwd=str(SCRIPT_ROOT), env=os.environ.copy(), check=False)
    if result.returncode:
        raise RuntimeError(f'{stage} exited with code {result.returncode}. Review the job log.')


def run_pdf_pipeline(jid: str, info: dict, root: Path, log):
    parts = ','.join(map(str, info['parts']))
    gen = [sys.executable, str(SCRIPT_ROOT / 'generate_physics_parts.py'),
           '--parts', parts, '--pdf', str(root / 'source' / 'workbook.pdf'),
           '--output', str(root / 'input'), '--model', info['model']]
    for opt, value in (('--reference', REFERENCE), ('--prompt', PROMPT),
                       ('--layout-prompt', LAYOUT_PROMPT), ('--sections', SECTIONS)):
        if value:
            gen.extend([opt, value])

    set_status(jid, status='running', stage='Preparing workbook', pid=os.getpid())
    if not SCRIPT_ROOT.joinpath('generate_physics_parts.py').is_file():
        raise RuntimeError(f'Generator not found in {SCRIPT_ROOT}')
    command(gen + ['--preview-sections'], log, 'Extracting question sections', jid)
    command(gen + ['--inventory-only'], log, 'Generating question inventory', jid)
    if info['stages'] in ('yaml', 'full'):
        command(gen, log, 'Generating solutions and YAML', jid)
        # Generator can sometimes finish after reporting failed question files.
        for p in info['parts']:
            report_path = root / 'input' / 'reports' / f'part_{p:02d}.json'
            if not report_path.exists():
                raise RuntimeError(f'Missing generation report for part {p}')
            report = json.loads(report_path.read_text(encoding='utf-8'))
            if not str(report.get('status', '')).startswith('passed_'):
                raise RuntimeError(f'Part {p} generation failed: {report.get("issues", [])}')
        command(gen + ['--validate-only'], log, 'Validating generated YAML', jid)
    if info['stages'] == 'full':
        if not RENDERER.is_file():
            raise RuntimeError(f'Renderer not found: {RENDERER}')
        render = [sys.executable, str(SCRIPT_ROOT / 'render_videos.py'),
                  '--parts', parts, '--input', str(root / 'input'),
                  '--output', str(root / 'videos'), '--renderer', str(RENDERER),
                  '--quality', info['quality']]
        command(render, log, 'Rendering narrated MP4s', jid)
        expected = [p for p in info['parts']
                    if list((root / 'videos' / f'part_{p:02d}_animations').rglob('*.mp4'))]
        # A custom narrator mux may output elsewhere; do not incorrectly mark all successful.
        if len(expected) != len(info['parts']):
            raise RuntimeError('Render command exited successfully but some MP4 files were not found under the video output directory. Inspect the narration mux destination and logs.')
        set_status(jid, status='completed', stage='Videos ready', completed_parts=expected)
    elif info['stages'] == 'yaml':
        set_status(jid, status='completed', stage='Validated YAML ready')
    else:
        set_status(jid, status='completed', stage='Inventory ready')


def run_image_extraction(jid: str, info: dict, root: Path, log):
    set_status(jid, status='running', stage='Recognizing questions', pid=os.getpid())
    extractor = SCRIPT_ROOT / 'image_question_extract.py'
    if not extractor.is_file():
        raise RuntimeError(f'Image question extractor not found: {extractor}')
    cmd = [sys.executable, str(extractor), '--job-root', str(root),
           '--output', str(root / 'input' / 'recognized_questions.json'),
           '--model', info['model']]
    command(cmd, log, 'Recognizing questions from images', jid)

    manifest_path = root / 'input' / 'recognized_questions.json'
    if not manifest_path.is_file():
        raise RuntimeError('Recognition completed without writing recognized_questions.json')
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    if not isinstance(manifest, dict) or not isinstance(manifest.get('questions'), list):
        raise RuntimeError('Recognition output is missing a valid questions array')
    if not manifest['questions']:
        raise RuntimeError('Recognition output contains no questions')
    set_status(jid, status='awaiting_review', stage='Questions ready for review',
               review_status='pending', pid=None)


def run_image_generation(jid: str, info: dict, root: Path, log):
    if info.get('review_status') != 'approved':
        raise RuntimeError('Image generation requires explicit review approval')
    manifest_path = root / 'input' / 'reviewed_questions.json'
    if not manifest_path.is_file():
        raise RuntimeError('Missing approved reviewed_questions.json')

    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    if not isinstance(manifest, dict) or not manifest.get('questions'):
        raise RuntimeError('Approved reviewed questions manifest is empty or invalid')

    set_status(jid, status='running', stage='Preparing approved questions', pid=os.getpid())
    generator = SCRIPT_ROOT / 'generate_physics_parts.py'
    if not generator.is_file():
        raise RuntimeError(f'Generator not found in {SCRIPT_ROOT}')

    gen = [sys.executable, str(generator), '--parts', '1',
           '--questions-json', str(manifest_path),
           '--job-root', str(root), '--output', str(root / 'input'),
           '--model', info['model']]
    for opt, value in (('--reference', REFERENCE), ('--prompt', PROMPT),
                       ('--layout-prompt', LAYOUT_PROMPT), ('--sections', SECTIONS)):
        if value:
            gen.extend([opt, value])

    command(gen, log, 'Generating solutions and YAML from approved questions', jid)

    report_path = root / 'input' / 'reports' / 'part_01.json'
    if not report_path.is_file():
        raise RuntimeError('Missing generation report for approved image questions')
    report = json.loads(report_path.read_text(encoding='utf-8'))
    if not str(report.get('status', '')).startswith('passed_'):
        raise RuntimeError(f'Approved image question generation failed: {report.get("issues", [])}')

    if not (root / 'input' / 'part_01_animations.yaml').is_file():
        raise RuntimeError('Missing generated image-question YAML')
    command(gen + ['--validate-only'], log, 'Validating generated YAML', jid)

    if info['stages'] == 'full':
        if not RENDERER.is_file():
            raise RuntimeError(f'Renderer not found: {RENDERER}')
        render = [sys.executable, str(SCRIPT_ROOT / 'render_videos.py'),
                  '--parts', '1', '--input', str(root / 'input'),
                  '--output', str(root / 'videos'), '--renderer', str(RENDERER),
                  '--quality', info['quality']]
        command(render, log, 'Rendering narrated MP4s', jid)
        if not list(root.joinpath('videos').rglob('*.mp4')):
            raise RuntimeError('Render command completed without producing an MP4 file')
        set_status(jid, status='completed', stage='Videos ready', completed_parts=[1])
    else:
        set_status(jid, status='completed', stage='Validated YAML ready', completed_parts=[1])


def main(jid: str, phase: str | None = None):
    root = job_dir(jid)
    info = read_job(jid)
    with (root / 'logs' / 'pipeline.log').open('a', encoding='utf-8', buffering=1) as log:
        try:
            input_kind = info.get('input_kind', 'pdf')
            if phase is not None and phase not in ('extract', 'generate'):
                raise ValueError(f'Invalid runner phase: {phase}')
            if input_kind not in ('pdf', 'image'):
                raise ValueError(f'Invalid job input_kind: {input_kind}')
            if input_kind == 'pdf' and phase is not None:
                raise ValueError('PDF jobs do not use extraction or generation phases')
            if input_kind == 'image' and phase is None:
                raise ValueError('Image jobs require an extract or generate phase')
            if input_kind == 'image':
                if phase == 'extract':
                    run_image_extraction(jid, info, root, log)
                else:
                    run_image_generation(jid, info, root, log)
            else:
                run_pdf_pipeline(jid, info, root, log)
        except Exception as exc:
            log.write(f'\nERROR: {exc}\n{traceback.format_exc()}\n')
            set_status(jid, status='failed', stage='Pipeline failed', error=str(exc))


if __name__ == '__main__':
    if len(sys.argv) not in (2, 3):
        raise SystemExit('Usage: python runner.py <job-id> [extract|generate]')
    main(sys.argv[1], sys.argv[2] if len(sys.argv) == 3 else None)

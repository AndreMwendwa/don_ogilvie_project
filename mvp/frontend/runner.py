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


def main(jid: str):
    root = job_dir(jid)
    info = read_job(jid)
    parts = ','.join(map(str, info['parts']))
    gen = [sys.executable, str(SCRIPT_ROOT / 'generate_physics_parts.py'),
           '--parts', parts, '--pdf', str(root / 'source' / 'workbook.pdf'),
           '--output', str(root / 'input'), '--model', info['model']]
    for opt, value in (('--reference', REFERENCE), ('--prompt', PROMPT),
                       ('--layout-prompt', LAYOUT_PROMPT), ('--sections', SECTIONS)):
        if value:
            gen.extend([opt, value])
    with (root / 'logs' / 'pipeline.log').open('a', encoding='utf-8', buffering=1) as log:
        try:
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
                expected = [p for p in info['parts'] if list((root / 'videos' / f'part_{p:02d}_animations').rglob('*.mp4'))]
                # A custom narrator mux may output elsewhere; do not incorrectly mark all successful.
                if len(expected) != len(info['parts']):
                    raise RuntimeError('Render command exited successfully but some MP4 files were not found under the video output directory. Inspect the narration mux destination and logs.')
                set_status(jid, status='completed', stage='Videos ready', completed_parts=expected)
            elif info['stages'] == 'yaml':
                set_status(jid, status='completed', stage='Validated YAML ready')
            else:
                set_status(jid, status='completed', stage='Inventory ready')
        except Exception as exc:
            log.write(f'\nERROR: {exc}\n{traceback.format_exc()}\n')
            set_status(jid, status='failed', stage='Pipeline failed', error=str(exc))


if __name__ == '__main__':
    if len(sys.argv) != 2:
        raise SystemExit('Usage: python runner.py <job-id>')
    main(sys.argv[1])

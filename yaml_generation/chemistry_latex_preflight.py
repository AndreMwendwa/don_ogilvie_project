"""Compile-check Manim LaTeX objects and optionally repair only failing YAML expressions.

Uses the identical MathTex/Tex constructors and mhchem template as chemistry_renderer.
Repairs are LLM-proposed, compile-checked, backed up, and never silently accepted.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import re
import sys
from pathlib import Path

import yaml

TYPES = {'mathtex', 'tex', 'chemical_equation'}
REPAIR_SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'required': ['content'], 'properties': {'content': {'type': 'string'}},
}


def iter_expressions(data):
    if not isinstance(data, dict) or not isinstance(data.get('questions'), dict):
        raise ValueError('YAML must have a questions mapping')
    for qid, question in data['questions'].items():
        if not isinstance(question, dict):
            continue
        objects = question.get('objects', {})
        if not isinstance(objects, dict):
            continue
        for oid, spec in objects.items():
            if not isinstance(spec, dict):
                continue
            kind = str(spec.get('type', '')).lower()
            if kind in TYPES:
                yield str(qid), str(oid), kind, str(spec.get('content', ''))


def compiler():
    try:
        from manim import MathTex, Tex, TexTemplate
    except ImportError as exc:
        raise RuntimeError('Manim is required for LaTeX preflight. Activate manim_env.') from exc
    template = TexTemplate()
    template.add_to_preamble(r'\usepackage[version=4]{mhchem}')

    def check(kind, content):
        if kind == 'tex':
            Tex(content)
        elif kind == 'mathtex':
            MathTex(content)
        elif kind == 'chemical_equation':
            expression = content.strip()
            if not expression.startswith(r'\ce{'):
                expression = r'\ce{' + expression + '}'
            MathTex(expression, tex_template=template)
        else:
            raise ValueError(f'Unexpected LaTeX object: {kind}')
    return check


def check_all(data, check):
    failures = []
    for qid, oid, kind, content in iter_expressions(data):
        try:
            check(kind, content)
        except Exception as exc:
            failures.append({'question': qid, 'object': oid, 'type': kind,
                             'content': content, 'error': str(exc)})
    return failures


def ask_repair(model, failure, api_key, timeout):
    import httpx
    prompt = (
        'You are repairing a single malformed LaTeX expression in a chemistry educational '
        'Manim YAML animation. Return JSON with exactly one field: content. '
        'Correct syntax only; preserve all chemistry, numbers, chemical identities, '
        'algebra, mathematical meaning, units and intended presentation. '
        'Use valid MathTex syntax for mathtex, valid Tex for tex, and valid mhchem '
        'notation for chemical_equation. For chemical_equation, the original content '
        'may be raw mhchem or wrapped in \\ce{...}; do not change the type. '
        'Never escape LaTeX backslashes twice in the resulting interpreted string. '
        'If the intended mathematical meaning is uncertain, preserve it instead of guessing.\n'
        + json.dumps(failure, ensure_ascii=False)
    )
    response = httpx.post('https://openrouter.ai/api/v1/chat/completions',
        headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'},
        json={'model': model, 'temperature': 0, 'messages': [{'role': 'user', 'content': prompt}],
              'response_format': {'type': 'json_object'}, 'max_tokens': 1000}, timeout=timeout)
    response.raise_for_status()
    content = response.json()['choices'][0]['message']['content']
    if isinstance(content, list):
        content = '\n'.join(x.get('text', '') for x in content if isinstance(x, dict))
    match = re.fullmatch(r'\s*```(?:json)?\s*(.*?)\s*```\s*', content, re.S)
    if match:
        content = match.group(1)
    parsed = json.loads(content)
    if not isinstance(parsed, dict) or not isinstance(parsed.get('content'), str):
        raise ValueError('Repair response missing string content')
    return parsed['content']


def preflight(path: Path, repair=False, model='openai/gpt-4.1', attempts=2,
              timeout=120, report_path=None, check=None, repair_fn=None):
    """Return (passed, report). Never mutate original file on failed repair.

    check/repair_fn injection permits offline tests without Manim/API.
    """
    data = yaml.safe_load(path.read_text(encoding='utf-8'))
    check = check or compiler()
    failures = check_all(data, check)
    report = {'file': str(path), 'checked': sum(1 for _ in iter_expressions(data)),
              'initial_failures': failures, 'repairs': [], 'remaining_failures': [], 'passed': False}
    updated = copy.deepcopy(data)
    if failures and repair:
        if repair_fn is None:
            key = os.getenv('OPENROUTER_API_KEY')
            if not key:
                raise RuntimeError('OPENROUTER_API_KEY is needed for --repair (not for validation).')
            repair_fn = lambda f: ask_repair(model, f, key, timeout)
        for failure in failures:
            obj = updated['questions'][next(k for k in updated['questions'] if str(k) == failure['question'])]['objects'][failure['object']]
            current = failure['content']
            for attempt in range(1, attempts + 1):
                try:
                    candidate = repair_fn({**failure, 'content': current})
                    if not candidate or candidate == current:
                        raise ValueError('Model returned unchanged/empty expression')
                    check(failure['type'], candidate)
                except Exception as exc:
                    report['repairs'].append({'question': failure['question'], 'object': failure['object'],
                                              'attempt': attempt, 'accepted': False, 'reason': str(exc)})
                    continue
                obj['content'] = candidate
                report['repairs'].append({'question': failure['question'], 'object': failure['object'],
                                          'attempt': attempt, 'accepted': True,
                                          'before': failure['content'], 'after': candidate})
                break
    remaining = check_all(updated, check)
    report['remaining_failures'] = remaining
    report['passed'] = not remaining
    # Commit only once ALL expressions pass (no partially repaired YAML).
    if repair and failures and not remaining:
        backup = path.with_suffix(path.suffix + '.bak')
        if not backup.exists():
            backup.write_bytes(path.read_bytes())
        temp = path.with_suffix(path.suffix + '.tmp')
        temp.write_text(yaml.safe_dump(updated, sort_keys=False, allow_unicode=True, width=105), encoding='utf-8')
        temp.replace(path)
        report['backup'] = str(backup)
    if report_path:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding='utf-8')
    return report['passed'], report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('yaml_file', type=Path)
    parser.add_argument('--repair', action='store_true', help='Repair invalid expressions using OpenRouter')
    parser.add_argument('--model', default=os.getenv('OPENROUTER_MODEL', 'openai/gpt-4.1'))
    parser.add_argument('--attempts', type=int, default=2)
    parser.add_argument('--timeout', type=float, default=120)
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    if args.attempts < 1:
        parser.error('--attempts must be >= 1')
    try:
        ok, report = preflight(args.yaml_file, repair=args.repair, model=args.model,
                               attempts=args.attempts, timeout=args.timeout, report_path=args.report)
    except Exception as exc:
        print(f'PREFLIGHT ERROR: {exc}', file=sys.stderr)
        return 2
    print(f"Preflight {'PASS' if ok else 'FAIL'}: {report['checked']} expressions, "
          f"{len(report['initial_failures'])} original failures, "
          f"{len(report['remaining_failures'])} remaining failures")
    for f in report['remaining_failures']:
        print(f"  Q{f['question']} / {f['object']} ({f['type']}): {f['content']!r}\n    {f['error']}")
    if report.get('backup'):
        print(f"Original backed up: {report['backup']}")
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())

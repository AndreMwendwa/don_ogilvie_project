"""Generate physics-workbook Manim YAML using independent validated OpenRouter API requests.

Python 3.10+; run --self-test without an API key.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import json
import logging
import os
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import yaml
from physics_extract import extract_section, load_sections, DEFAULT_SECTIONS
from openrouter_client import OpenRouterClient

ROOT = Path(__file__).resolve().parent
LOG = logging.getLogger("manim-generator")
INVENTORY_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["part", "section_title", "questions"],
    "properties": {
        "part": {"type": "integer"},
        "section_title": {"type": "string"},
        "questions": {
            "type": "array", "items": {"type": "object", "additionalProperties": False,
            "required": ["id", "problem", "source_page", "notes"],
            "properties": {
                "id": {"type": "string"},
                "problem": {"type": "string"},
                "source_page": {"type": "integer"},
                "notes": {"type": "string"}
            }}
        }
    }
}
QUESTION_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["question_yaml"],
    "properties": {"question_yaml": {"type": "string"}}
}
REVIEW_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["approved", "issues"],
    "properties": {
        "approved": {"type": "boolean"},
        "issues": {"type": "array", "items": {"type": "string"}}
    }
}


def dump_yaml(data: Any) -> str:
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=105)


def write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


def read_yaml(path: Path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def extract_actions(question: dict) -> set[str]:
    found = set()
    for action in question.get("actions", []):
        if isinstance(action, dict):
            found.add(action.get("action"))
            for anim in action.get("animations", []):
                if isinstance(anim, dict):
                    found.add(anim.get("action"))
    return found - {None}


def schema_from_reference(reference: dict) -> dict:
    assert isinstance(reference, dict) and isinstance(reference.get("questions"), dict)
    examples = reference["questions"]
    allowed_types = set()
    allowed_actions = set()
    example_keys = set()
    for q in examples.values():
        for obj in q.get("objects", {}).values():
            allowed_types.add(obj.get("type"))
        allowed_actions.update(extract_actions(q))
        example_keys.update(q.keys())
    return {
        "allowed_types": allowed_types - {None},
        "allowed_actions": allowed_actions,
        "question_keys": example_keys,
        "example": next(iter(examples.values()))
    }


def latex_warnings(s: str, label: str) -> list[str]:
    issues = []
    balance = 0
    for i, ch in enumerate(s):
        if ch == "{" and (i == 0 or s[i-1] != "\\"):
            balance += 1
        elif ch == "}" and (i == 0 or s[i-1] != "\\"):
            balance -= 1
            if balance < 0:
                issues.append(f"{label}: unmatched closing brace")
                break
    if balance > 0:
        issues.append(f"{label}: unmatched opening brace")
    if s.count(r"\left") != s.count(r"\right"):
        issues.append(f"{label}: unmatched \\left/\\right")
    if "\t" in s or "\n" in s:
        issues.append(f"{label}: literal control character, possible LaTeX escaping error")
    return issues


def validate_question(question: dict, spec: dict) -> list[str]:
    errors = []
    if not isinstance(question, dict):
        return ["question must be a mapping"]
    if not isinstance(question.get("objects"), dict) or not question["objects"]:
        return ["objects must be a nonempty mapping"]
    if not isinstance(question.get("actions"), list) or not question["actions"]:
        return ["actions must be a nonempty list"]
    unknown_q_keys = set(question) - spec["question_keys"]
    if unknown_q_keys:
        errors.append(f"unsupported question fields: {sorted(unknown_q_keys)}")
    objects = question["objects"]
    for objid, obj in objects.items():
        if not isinstance(obj, dict):
            errors.append(f"object {objid!r} is not a mapping")
            continue
        if obj.get("type") not in spec["allowed_types"]:
            errors.append(f"object {objid!r}: unrecognized type {obj.get('type')!r}")
        if not isinstance(obj.get("content"), str) or not obj.get("content", "").strip():
            errors.append(f"object {objid!r}: missing content")
        if obj.get("type") == "mathtex":
            errors += latex_warnings(obj.get("content", ""), str(objid))
        position = obj.get("position")
        if isinstance(position, dict) and "move_to" in position:
            coord = position["move_to"]
            if (not isinstance(coord, list) or len(coord) != 3 or
                not all(isinstance(x, (int, float)) for x in coord)):
                errors.append(f"object {objid!r}: move_to must contain three numbers")
            elif abs(coord[0]) > 7.5 or abs(coord[1]) > 4.5:
                errors.append(f"object {objid!r}: move_to likely outside 16:9 scene")
    shown = set()
    for index, a in enumerate(question["actions"]):
        if not isinstance(a, dict):
            errors.append(f"actions[{index}] must be a mapping")
            continue
        animations = a.get("animations", []) if a.get("action") == "play" else [a]
        if not isinstance(animations, list) or not animations:
            errors.append(f"actions[{index}] has no animations")
            continue
        for anim in animations:
            if not isinstance(anim, dict):
                errors.append(f"actions[{index}]: animation must be a mapping")
                continue
            kind = anim.get("action")
            if kind not in spec["allowed_actions"]:
                errors.append(f"actions[{index}]: unsupported action {kind!r}")
            target = anim.get("target")
            if target is not None and target not in objects:
                errors.append(f"actions[{index}]: missing target object {target!r}")
            if kind in {"write", "fade_in"} and target is not None:
                shown.add(target)
            if kind == "highlight" and target not in shown:
                errors.append(f"actions[{index}]: highlight before showing {target!r}")
        voiceover = a.get("voiceover")
        if voiceover is not None and (not isinstance(voiceover, str) or not voiceover.strip()):
            errors.append(f"actions[{index}]: blank voiceover")
    source_object = "problem" if "problem" in objects else "line_0" if "line_0" in objects else None
    if source_object is None:
        errors.append("missing source question display: expected 'problem' or 'line_0'")
    elif source_object not in shown:
        errors.append(f"source object {source_object!r} is never introduced")
    return errors


def validate_part(data: dict, inventory: dict, spec: dict, reference: dict) -> list[str]:
    errors = []
    if not isinstance(data, dict):
        return ["YAML root is not a mapping"]
    if set(data) != set(reference):
        errors.append(f"top-level keys differ: {set(data)} versus {set(reference)}")
    if data.get("video") != reference.get("video"):
        errors.append("video configuration differs from reference")
    questions = data.get("questions")
    if not isinstance(questions, dict):
        return errors + ["missing questions mapping"]
    expected = {str(q["id"]) for q in inventory["questions"]}
    actual = {str(k) for k in questions}
    if actual != expected:
        errors.append(f"question coverage mismatch: missing={sorted(expected-actual)}, extra={sorted(actual-expected)}")
    for k, q in questions.items():
        errors += [f"question {k}: {err}" for err in validate_question(q, spec)]
    return errors


def normalize_question_id(value: str):
    # Keep reference YAML's integer question keys where possible.
    s = str(value).strip()
    return int(s) if re.fullmatch(r"[1-9]\d*", s) else s


def validate_inventory(data: dict, part: int) -> None:
    if data.get("part") != part:
        raise ValueError(f"inventory returned part {data.get('part')} instead of {part}")
    qs = data.get("questions")
    if not isinstance(qs, list) or not qs:
        raise ValueError("inventory has no questions")
    ids = [q.get("id", "") for q in qs]
    if any(not str(i).strip() for i in ids) or len(set(ids)) != len(ids):
        raise ValueError(f"duplicate / blank question ids: {ids}")
    expected_ids = {str(n) for n in range(1, 26)}
    if {str(x) for x in ids} != expected_ids:
        raise ValueError(f"Part {part}: expected printed question IDs 1-25, got {ids}")
    for q in qs:
        if not str(q.get("problem", "")).strip():
            raise ValueError(f"missing source problem: {q['id']}")


async def api_json(client: OpenRouterClient, model: str, schema: dict,
                   name: str, instruction: str) -> dict:
    """Use the shared OpenRouterClient JSON-schema/fallback transport."""
    return await client.json(model=model, schema=schema, name=name, text=instruction)


async def get_inventory(client, model, part, dest, master, force, section, section_text):
    if dest.exists() and not force:
        inv = json.loads(dest.read_text(encoding="utf-8"))
        validate_inventory(inv, part)
        if len(inv['questions']) != section['expected_questions']:
            raise ValueError(f"Cached inventory question count mismatch for part {part}; use --force")
        return inv
    instruction = f"""{master}

TASK: QUESTION INVENTORY ONLY for PHYSICS WORKBOOK PART {part}: {section['title']}.
This is {section['kind']} format. Extract ONLY questions from the delimited student-workbook text below. Do not scan the rest of the PDF for additional items; instructor answers are NOT questions.
Include exactly {section['expected_questions']} numbered problems, one entry per numbered item. Include every subpart of a numbered question within the same entry. If a multiple-choice problem, include the full options A/B/C/D.
Use 'id' as the printed question integer converted to a string, 'problem' as the complete printed question (mathematical symbols faithfully transcribed to LaTeX when needed), 'source_page' as the ONE-BASED PDF page index indicated by [PDF PAGE N] at question start, and 'notes' for extraction errors, image dependency, or unrecoverable ambiguity; otherwise blank.
Do not write solutions. Do not assume question numbering corresponds to equations in the Given Equations / curriculum section.

--- BEGIN STUDENT QUESTION SECTION ---
{section_text}
--- END STUDENT QUESTION SECTION ---
Return structured JSON only."""
    inv = await api_json(client, model, INVENTORY_SCHEMA, "question_inventory",
                         instruction)
    validate_inventory(inv, part)
    if len(inv['questions']) != section['expected_questions']:
        raise ValueError(f"Part {part}: expected {section['expected_questions']} questions, got {len(inv['questions'])}")
    write_atomic(dest, json.dumps(inv, indent=2, ensure_ascii=False))
    return inv


async def generate_question(client, model, part, entry, reference, spec,
                            master, output, force, math_review, max_repairs):
    ident = entry["id"]
    if output.exists() and not force:
        try:
            existing = read_yaml(output)
            if not validate_question(existing, spec):
                return existing
        except Exception:
            pass
    exemplar = dump_yaml({"questions": {1: spec["example"]}})
    errors = []
    candidate = None
    for attempt in range(max_repairs + 1):
        prompt = f"""{master}

TASK: Generate ONE complete Manim YAML question definition for PHYSICS WORKBOOK SECTION {part}, question ID {ident}.
EXACT SOURCE PROBLEM (approved inventory): {entry['problem']}
SOURCE PAGE: {entry['source_page']}; SOURCE NOTES: {entry['notes']}

REFERENCE SINGLE-QUESTION YAML:
{exemplar}

Create only a mapping with two keys: 'objects' and 'actions', NOT a 'questions' wrapper and NOT 'video'. Use YAML within the 'question_yaml' structured JSON field.
ALLOWED OBJECT TYPES: {sorted(spec['allowed_types'])}
ALLOWED ACTION NAMES: {sorted(spec['allowed_actions'])}
Ensure 'problem' object states the original full problem, with exact exponents; use title, problem_label, problem, step_1, step_2, ... if possible.
Display each meaningful step in sequence, narrate each step, and highlight final answer. Preserve source conditions and correct domain restrictions.
Do not simply claim the source result; solve and verify independently.
"""
        if errors:
            prompt += ("\nPREVIOUS CANDIDATE WAS REJECTED. Correct ALL issues without losing valid work. "
                       + "\nValidation/review issues:\n" + "\n".join(errors[:30])
                       + "\nPrevious YAML:\n" + (candidate or "")[:22000])
        result = await api_json(client, model, QUESTION_SCHEMA, "animation_question",
                                prompt)
        candidate = result["question_yaml"].strip()
        if candidate.startswith("```"):
            candidate = re.sub(r"^```(?:yaml)?\s*", "", candidate)
            candidate = re.sub(r"\s*```$", "", candidate)
        try:
            question = yaml.safe_load(candidate)
            errors = validate_question(question, spec)
        except Exception as exc:
            errors = [f"Invalid YAML: {exc}"]
            question = None
        if not errors and math_review:
            review_prompt = f"""You are an INDEPENDENT rigorous physics reviewer. Check units, sign conventions, assumptions, physical reasonableness, significant figures, and every multi-part result. The original question from the PDF is:
{entry['problem']}
Review this proposed Manim animation question definition independently for mathematical correctness, domain assumptions, transformations, transcription, final result, and fidelity of explanations. DO NOT approve if a materially wrong mathematical step or a missing/incorrect result is present. Output JSON with approved=true/false and precise actionable issues (empty when approved). Treat the source PDF as authoritative for original question statements but independently verify its mathematical claims. YAML follows:\n{candidate}"""
            review = await api_json(client, model, REVIEW_SCHEMA, "math_review",
                                    review_prompt)
            if not review["approved"]:
                errors = ["MATH REVIEW: " + x for x in review["issues"]] or ["math review rejected without explanation"]
        if not errors:
            write_atomic(output, dump_yaml(question))
            LOG.info("Part %s question %s accepted", part, ident)
            return question
        LOG.warning("Part %s question %s rejected attempt %s: %s", part, ident, attempt + 1, errors)
    raise ValueError(f"Part {part} question {ident} failed validation after repair: {errors}")


async def process_part(part: int, args, client, reference, spec, master, section, section_text,
                       part_semaphore, question_semaphore) -> dict:
    async with part_semaphore:
        LOG.info("PART %02d started", part)
        out = args.output
        inventory_path = out / "inventories" / f"part_{part:02d}.json"
        path = out / f"part_{part:02d}_animations.yaml"
        report_path = out / "reports" / f"part_{part:02d}.json"
        report = {"part": part, "status": "failed", "issues": []}
        try:
            inv = await get_inventory(client, args.model, part,
                                      inventory_path, master, args.force, section, section_text)
            if args.inventory_only:
                report.update({"status": "inventory_created", "questions": len(inv["questions"]), "file": str(inventory_path)})
                return report
            jobs = []
            for q in inv["questions"]:
                # IDs used in fragment filenames can include punctuation.
                slug = re.sub(r"[^A-Za-z0-9_.-]", "_", q["id"])
                frag = out / "fragments" / f"part_{part:02d}" / f"{slug}.yaml"
                async def job(entry=q, target=frag):
                    async with question_semaphore:
                        try:
                            val = await generate_question(
                                client, args.model, part, entry, reference,
                                spec, master, target, args.force,
                                not args.skip_math_review, args.max_repairs)
                            return entry["id"], val, None
                        except Exception as exc:
                            return entry["id"], None, str(exc)
                jobs.append(job())
            outcomes = await asyncio.gather(*jobs)
            failed = [f"{qid}: {err}" for qid, _, err in outcomes if err]
            if failed:
                report["issues"] = failed
                return report
            data = {
                "video": copy.deepcopy(reference["video"]),
                "questions": {normalize_question_id(qid): q for qid, q, _ in outcomes}
            }
            problems = validate_part(data, inv, spec, reference)
            if problems:
                report["issues"] = problems
                return report
            write_atomic(path, dump_yaml(data))
            report.update({"status": "passed_structural_and_math_review" if not args.skip_math_review
                           else "passed_structural_only", "questions": len(inv["questions"]),
                           "file": str(path), "issues": []})
            LOG.info("PART %02d finished: %s questions", part, len(inv["questions"]))
            return report
        except Exception as exc:
            LOG.exception("PART %02d failed", part)
            report["issues"] = [str(exc)]
            return report
        finally:
            write_atomic(report_path, json.dumps(report, indent=2, ensure_ascii=False))


def parse_parts(spec: str) -> list[int]:
    values = []
    for segment in spec.split(","):
        segment = segment.strip()
        if "-" in segment:
            left, right = segment.split("-", 1)
            values += list(range(int(left), int(right)+1))
        else:
            values.append(int(segment))
    if any(v < 1 for v in values):
        raise ValueError("part numbers must be positive")
    return list(dict.fromkeys(values))


def self_test(reference, spec):
    q = copy.deepcopy(spec["example"])
    for k, reference_q in reference["questions"].items():
        result = validate_question(reference_q, spec)
        if result:
            raise AssertionError(f"Reference question {k} did not pass: {result}")
    bad = copy.deepcopy(q)
    bad["actions"][0]["animations"][0]["target"] = "nonexistent_object"
    assert any("missing target" in x for x in validate_question(bad, spec))
    print(f"Self-test passed: all {len(reference['questions'])} reference questions are valid and bad object reference is detected.")


async def main(args):
    args.output.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout),
                                  logging.FileHandler(args.output / "logs.txt", encoding="utf-8")])
    reference = read_yaml(args.reference)
    spec = schema_from_reference(reference)
    LOG.info("Reference: %s object types, %s action names", sorted(spec["allowed_types"]),
             sorted(spec["allowed_actions"]))
    if args.self_test:
        self_test(reference, spec)
        return 0
    if args.validate_only:
        failure_count = 0
        for part in args.parts:
            path = args.output / f"part_{part:02d}_animations.yaml"
            invpath = args.output / "inventories" / f"part_{part:02d}.json"
            try:
                inv = json.loads(invpath.read_text(encoding="utf-8"))
                issues = validate_part(read_yaml(path), inv, spec, reference)
            except Exception as exc:
                issues = [str(exc)]
            print(f"PART {part}: " + ("PASS" if not issues else f"FAIL {issues}"))
            failure_count += bool(issues)
        return bool(failure_count)
    sections = load_sections(args.sections)
    if any(p not in sections for p in args.parts):
        raise ValueError(f"Selected part is not in {args.sections}; available: {sorted(sections)}")
    extracted = {p: extract_section(args.pdf, sections[p]) for p in args.parts}
    if args.preview_sections:
        folder = args.output / 'section_previews'
        folder.mkdir(parents=True, exist_ok=True)
        for part, text in extracted.items():
            dest=folder / f'part_{part:02d}_questions.txt'
            write_atomic(dest, text)
            print(f"Part {part}: {sections[part]['title']}, {len(text)} chars, {dest}")
        return 0
    if not os.getenv("OPENROUTER_API_KEY"):
        raise RuntimeError("OPENROUTER_API_KEY is not set (set in environment, never in script).")
    client = OpenRouterClient(
        api_key=os.environ["OPENROUTER_API_KEY"],
        timeout=args.timeout,
        format_mode=args.format_mode,
        pdf_engine=args.pdf_engine,
        max_tokens=args.max_tokens,
        max_retries=args.api_retries,
    )
    master = args.prompt.read_text(encoding="utf-8")
    LOG.info("Using locally extracted PDF question text; no PDF uploaded to any API")
    part_semaphore = asyncio.Semaphore(args.concurrency)
    question_semaphore = asyncio.Semaphore(args.question_concurrency)
    try:
        reports = await asyncio.gather(*(process_part(
            p, args, client, reference, spec, master, sections[p], extracted[p],
            part_semaphore, question_semaphore) for p in args.parts))
        for r in reports:
            print(f"Part {r['part']}: {r['status']}" + (f" {r['issues']}" if r["issues"] else ""))
        if client.usage:
            usage_path = args.output / "openrouter_usage.json"
            write_atomic(usage_path, json.dumps(client.usage, indent=2, ensure_ascii=False))
            LOG.info("OpenRouter usage saved to %s", usage_path)
        return int(any(r["status"] == "failed" for r in reports))
    finally:
        await client.close()


def arguments():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--parts", type=parse_parts, default=parse_parts("1-7"))
    p.add_argument("--pdf", type=Path, default=ROOT / "Physics-Grade10-12_tips_tricks.pdf")
    p.add_argument("--reference", type=Path, default=ROOT / "part_01_animations.yaml")
    p.add_argument("--prompt", type=Path, default=ROOT / "physics_master_prompt.txt")
    p.add_argument("--output", type=Path, default=ROOT / "physics_output")
    p.add_argument("--sections", type=Path, default=DEFAULT_SECTIONS)
    p.add_argument("--preview-sections", action="store_true", help="Offline: save question-only page excerpts, no API key needed")
    p.add_argument("--model", default=os.getenv("OPENROUTER_MODEL", "openai/gpt-4.1"))
    p.add_argument("--format-mode", choices=["auto", "json_schema", "json_object", "prompt"],
                   default="auto", help="OpenRouter JSON response format strategy")
    p.add_argument("--max-tokens", type=int, default=12000, help="Maximum model output tokens per request")
    p.add_argument("--timeout", type=float, default=600, help="OpenRouter request timeout in seconds")
    p.add_argument("--api-retries", type=int, default=3, help="Retries per output format")
    p.add_argument("--pdf-engine", default="cloudflare-ai", help="Used only for PDF inputs to OpenRouterClient")
    p.add_argument("--concurrency", type=int, default=3)
    p.add_argument("--question-concurrency", type=int, default=2)
    p.add_argument("--max-repairs", type=int, default=2)
    p.add_argument("--skip-math-review", action="store_true")
    p.add_argument("--force", action="store_true")
    p.add_argument("--validate-only", action="store_true")
    p.add_argument("--inventory-only", action="store_true", help="Generate inventories without paid solution generation")
    p.add_argument("--self-test", action="store_true")
    return p.parse_args()


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main(arguments())))
    except (KeyboardInterrupt, Exception) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

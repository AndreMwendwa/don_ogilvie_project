"""Generate Manim YAML using independent OpenRouter requests (any text-output model).

Python 3.10+; run --self-test without an API key.
"""
from __future__ import annotations

import argparse
import hashlib
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
    for q in qs:
        if not str(q.get("problem", "")).strip():
            raise ValueError(f"missing source problem: {q['id']}")


async def api_json(client, model, schema, name, prompt, pdf=None):
    return await client.json(model, schema, name, prompt, pdf=pdf)


def pdf_bytes_for_page(pdf_path: Path, page_number: int, cache: dict) -> bytes:
    """Small one-page PDF, avoiding repeatedly sending the complete book."""
    if page_number not in cache:
        import fitz
        with fitz.open(pdf_path) as src:
            if not (1 <= page_number <= src.page_count):
                raise ValueError(f"Invalid source page {page_number}; PDF has {src.page_count} pages")
            out = fitz.open()
            out.insert_pdf(src, from_page=page_number - 1, to_page=page_number - 1)
            cache[page_number] = out.tobytes(garbage=3, deflate=True)
            out.close()
    return cache[page_number]


def source_page_text(pdf_path: Path, page_number: int) -> str:
    import fitz
    with fitz.open(pdf_path) as doc:
        if not (1 <= page_number <= doc.page_count):
            return ''
        return doc[page_number - 1].get_text()[:20000]


async def get_inventory(client, model, pdf_source, part, dest, master, force):
    if dest.exists() and not force:
        inv = json.loads(dest.read_text(encoding="utf-8"))
        validate_inventory(inv, part)
        return inv
    instruction = f"""{master}

TASK: QUESTION INVENTORY ONLY. Enumerate EVERY question and subquestion in TOP-LEVEL PART {part} of the provided PDF.
Read the table of contents and section headings before extracting; internal subsection names like 'Part 1' are not top-level parts. For Part 10, use 10A questions, with 10B solely for clarifying provided solutions.
Use each question's printed label as its id (e.g. '1', '2', or '1(a)'); if multiple subquestions share one numbered item, inventory them separately using distinct IDs like '1(a)' and '1(b)'.
Copy the exact mathematical expression into 'problem' using LaTeX (single escaped string), retaining operators, fractions, exponent placement and conditions. 'source_page' is the 1-based PDF page index. 'notes' records uncertain transcription, source ambiguities, or source solutions if essential; otherwise blank.
If a problem has a multi-line stem required by subquestions, include the stem in each associated problem entry. Do not invent questions, include worked examples not designated as questions, or answer the problems. Return structured JSON only."""
    inv = await api_json(client, model, INVENTORY_SCHEMA, "question_inventory",
                         instruction, pdf=pdf_source)
    validate_inventory(inv, part)
    write_atomic(dest, json.dumps(inv, indent=2, ensure_ascii=False))
    return inv


async def generate_question(client, model, review_model, pdf_path, pdf_cache, source_mode, part, entry, reference, spec,
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
    page_pdf = pdf_bytes_for_page(pdf_path, entry["source_page"], pdf_cache) if source_mode == "page-pdf" else None
    page_text = source_page_text(pdf_path, entry["source_page"]) if source_mode == "page-text" else ""
    for attempt in range(max_repairs + 1):
        prompt = f"""{master}

TASK: Generate ONE complete Manim YAML question definition for TOP-LEVEL PART {part}, question ID {ident}.
EXACT SOURCE PROBLEM (approved inventory): {entry['problem']}
SOURCE PAGE: {entry['source_page']}; SOURCE NOTES: {entry['notes']}
SOURCE PAGE EXTRACT (may lose mathematical formatting; prefer PDF/inventory): {page_text}

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
                                prompt, pdf=page_pdf)
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
            review_prompt = f"""You are an INDEPENDENT rigorous mathematics reviewer. The original question from the PDF is:
{entry['problem']}
Review this proposed Manim animation question definition independently for mathematical correctness, domain assumptions, transformations, transcription, final result, and fidelity of explanations. DO NOT approve if a materially wrong mathematical step or a missing/incorrect result is present. Output JSON with approved=true/false and precise actionable issues (empty when approved). Treat the source PDF as authoritative for original question statements but independently verify its mathematical claims. YAML follows:\n{candidate}"""
            review = await api_json(client, review_model, REVIEW_SCHEMA, "math_review", review_prompt)
            if not review["approved"]:
                errors = ["MATH REVIEW: " + x for x in review["issues"]] or ["math review rejected without explanation"]
        if not errors:
            write_atomic(output, dump_yaml(question))
            LOG.info("Part %s question %s accepted", part, ident)
            return question
        LOG.warning("Part %s question %s rejected attempt %s: %s", part, ident, attempt + 1, errors)
    raise ValueError(f"Part {part} question {ident} failed validation after repair: {errors}")


async def process_part(part: int, args, client, pdf_source, pdf_cache, reference, spec, master,
                       part_semaphore, question_semaphore) -> dict:
    async with part_semaphore:
        LOG.info("PART %02d started", part)
        out = args.output
        inventory_path = out / "inventories" / f"part_{part:02d}.json"
        path = out / f"part_{part:02d}_animations.yaml"
        report_path = out / "reports" / f"part_{part:02d}.json"
        report = {"part": part, "status": "failed", "issues": []}
        try:
            inv = await get_inventory(client, args.inventory_model or args.model, pdf_source, part,
                                      inventory_path, master, args.force)
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
                                client, args.generation_model or args.model, args.review_model or args.model,
                                args.pdf, pdf_cache, args.question_source, part, entry, reference,
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
    if any(v < 1 or v > 13 for v in values):
        raise ValueError("part numbers must be from 1 to 13")
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
    if not os.getenv("OPENROUTER_API_KEY"):
        raise RuntimeError("Set OPENROUTER_API_KEY; do not embed secrets in source code.")
    client = OpenRouterClient(
        api_key=os.environ["OPENROUTER_API_KEY"], format_mode=args.format_mode,
        pdf_engine=args.pdf_engine, max_tokens=args.max_tokens,
        max_retries=args.api_retries, timeout=args.timeout)
    master = args.prompt.read_text(encoding="utf-8")
    if args.list_models is not None:
        models = await client.list_models(args.list_models)
        for m in models[:100]:
            print(m.get('id'), 'context:', m.get('context_length'))
        await client.close()
        return 0
    pdf_source = args.pdf.read_bytes()
    pdf_cache = {}
    LOG.info("OpenRouter PDF source: %s (%s bytes)", args.pdf.name, len(pdf_source))
    part_semaphore = asyncio.Semaphore(args.concurrency)
    question_semaphore = asyncio.Semaphore(args.question_concurrency)
    reports = await asyncio.gather(*(process_part(
        p, args, client, pdf_source, pdf_cache, reference, spec, master,
        part_semaphore, question_semaphore) for p in args.parts))
    for r in reports:
        print(f"Part {r['part']}: {r['status']}" + (f" {r['issues']}" if r["issues"] else ""))
    write_atomic(args.output / "api_usage.json", json.dumps(client.usage, indent=2))
    await client.close()
    return int(any(r["status"] == "failed" for r in reports))


def arguments():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--parts", type=parse_parts, default=parse_parts("3-13"))
    p.add_argument("--pdf", type=Path, default=ROOT / "Maths-Grade8-12-Tips_Tricks.pdf")
    p.add_argument("--reference", type=Path, default=ROOT / "part_01_animations.yaml")
    p.add_argument("--prompt", type=Path, default=ROOT / "master_prompt.txt")
    p.add_argument("--output", type=Path, default=ROOT / "output")
    p.add_argument("--model", default=os.getenv("OPENROUTER_MODEL", "openai/gpt-5.6-sol"),
                   help="Any OpenRouter text-output model slug")
    p.add_argument("--inventory-model", default=None, help="Override model for inventories")
    p.add_argument("--generation-model", default=None, help="Override model for solutions")
    p.add_argument("--review-model", default=None, help="Override model for math review")
    p.add_argument("--format-mode", choices=["auto", "json_schema", "json_object", "prompt"], default="auto",
                   help="auto tries strict schema, JSON object, then prompt-only JSON")
    p.add_argument("--pdf-engine", choices=["cloudflare-ai", "mistral-ocr", "native"],
                   default="cloudflare-ai")
    p.add_argument("--question-source", choices=["page-pdf", "page-text", "inventory"],
                   default="page-pdf", help="Source sent to question generation; page-pdf best for superscripts")
    p.add_argument("--max-tokens", type=int, default=12000)
    p.add_argument("--timeout", type=float, default=600)
    p.add_argument("--api-retries", type=int, default=3)
    p.add_argument("--list-models", nargs="?", const="", default=None, metavar="SEARCH",
                   help="List OpenRouter models, optionally filter by slug; does not generate")
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

"""Page-aware extraction of question-only sections from the physics workbook.

Uses the PDF's printed section markers and one-based PDF page numbers. Does not
use OCR, infer missing expressions, or mix instructor answers into question input.
"""
from __future__ import annotations
import argparse
import json
import re
from pathlib import Path
import fitz

ROOT = Path(__file__).resolve().parent
DEFAULT_SECTIONS = ROOT / "physics_sections.json"

def flatten(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().casefold()

def locate_marker(text: str, marker: str, *, start: int = 0) -> int:
    # Page text generally puts the heading on one line, but may have line breaks.
    # Find literal marker while allowing whitespace and unicode dash variation.
    escaped = re.escape(marker).replace(r"\ ", r"\s+")
    escaped = re.sub('[—–]', '[—–-]', escaped)
    match = re.search(escaped, text[start:], flags=re.IGNORECASE)
    if match is None:
        raise ValueError(f"Could not find PDF section marker {marker!r}")
    return start + match.start()

def extract_section(pdf: Path, section: dict) -> str:
    with fitz.open(pdf) as doc:
        first, last = section['start_page'], section['end_page']
        if first < 1 or last > len(doc) or first > last:
            raise ValueError(f"Invalid page range {first}-{last} for {pdf}")
        pages = [(p, doc[p-1].get_text(sort=True)) for p in range(first, last+1)]
        content = '\n'.join(f"\n[PDF PAGE {number}]\n{page}" for number, page in pages)
        beginning = locate_marker(content, section['start_marker'])
        ending = locate_marker(content, section['end_marker'], start=beginning+len(section['start_marker']))
        text = f"[PDF PAGE {first}]\n" + content[beginning:ending].strip()
        if not text:
            raise ValueError(f"No questions extracted for part {section['part']}")
        return text

def load_sections(config: Path = DEFAULT_SECTIONS) -> dict[int, dict]:
    raw=json.loads(config.read_text(encoding='utf-8'))
    return {s['part']:s for s in raw['sections']}

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--pdf',type=Path,default=ROOT/'Physics-Grade10-12_tips_tricks.pdf')
    p.add_argument('--sections',type=Path,default=DEFAULT_SECTIONS)
    p.add_argument('--output',type=Path,default=ROOT/'section_previews')
    p.add_argument('--parts',default='1-7',help='comma-separated section IDs or ranges')
    args=p.parse_args(); config=load_sections(args.sections)
    selected=[]
    for term in args.parts.split(','):
        if '-' in term:
            a,b=map(int,term.split('-',1)); selected.extend(range(a,b+1))
        else:selected.append(int(term))
    args.output.mkdir(exist_ok=True,parents=True)
    for number in dict.fromkeys(selected):
        sec=config[number]
        text=extract_section(args.pdf,sec)
        path=args.output/f'part_{number:02d}_questions.txt'
        path.write_text(text,encoding='utf-8')
        print(f"Part {number}: {sec['title']} | pages {sec['start_page']}-{sec['end_page']} | {len(text)} characters | {path}")
if __name__=='__main__': main()

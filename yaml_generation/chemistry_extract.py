"""Extract student-only chemistry module questions from a 167-page combined workbook.

There are duplicate editions in the source. Configuration explicitly selects each occurrence
with one-based PDF page numbers; never use instructor answers as question input.
"""
from __future__ import annotations
import argparse,json,re
from pathlib import Path
import fitz
ROOT=Path(__file__).resolve().parent
DEFAULT_SECTIONS=ROOT/'chemistry_sections.json'

def load_sections(config=DEFAULT_SECTIONS):
    raw=json.loads(Path(config).read_text(encoding='utf8'))
    return {int(sec['part']):sec for sec in raw['sections']}

def extract_section(pdf, section):
    page=int(section['start_page'])
    with fitz.open(pdf) as doc:
        if not 1<=page<=len(doc): raise ValueError(f'Bad module page {page}')
        contents=[]
        for i in range(page-1, min(page+int(section.get('max_pages',4))-1,len(doc))):
            text=doc[i].get_text(sort=True)
            if i==page-1:
                marker=section.get('start_marker','')
                pos=text.casefold().find(marker.casefold())
                if pos<0: raise ValueError(f'Module {section["part"]}: start marker not found on page {page}: {marker}')
                text=text[pos:]
            # Stop before instructor solutions, sometimes within the starting page itself.
            boundary=re.search(r'(?im)^\s*(?:MODULE\s*\d+\s*[—–-]\s*)?(?:INSTRUCTOR\s+(?:SOLUTIONS?|VERSION|KEY)|FULL INSTRUCTOR SOLUTIONS|Instructor step-by-step solutions|Instructor solutions|4\.\s+Instructor Solutions)',text)
            if boundary: text=text[:boundary.start()]
            # Stop before any following module, if page contents cross a module boundary.
            next_module=re.search(r'(?im)^\s*MODULE\s+(\d+)\s*[—–-]',text)
            if next_module and int(next_module.group(1))!=section['part']:
                text=text[:next_module.start()]
                contents.append(f'[PDF PAGE {i+1}]\n{text}')
                break
            contents.append(f'[PDF PAGE {i+1}]\n{text}')
            if boundary: break
        result='\n'.join(contents).strip()
        found=set(re.findall(r'(?m)\bQ\s*(\d{1,2})\s*[.\-:]?',result))
        if len(found)<int(section.get('expected_questions',10)):
            raise ValueError(f'Module {section["part"]}: extracted only Q{sorted(found)}. Review source, scanned pages or markers; nothing inferred.')
        return result

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--pdf',type=Path,default=ROOT/'Chemistry-Module 1-14.pdf')
    p.add_argument('--sections',type=Path,default=DEFAULT_SECTIONS)
    p.add_argument('--parts',default='1-14')
    p.add_argument('--output',type=Path,default=ROOT/'chemistry_output/section_previews')
    a=p.parse_args(); conf=load_sections(a.sections);a.output.mkdir(exist_ok=True,parents=True)
    parts=[]
    for token in a.parts.split(','):
        if '-' in token:
            x,y=map(int,token.split('-',1));parts.extend(range(x,y+1))
        else:parts.append(int(token))
    for part in dict.fromkeys(parts):
        text=extract_section(a.pdf,conf[part]);path=a.output/f'part_{part:02d}_questions.txt'
        path.write_text(text,encoding='utf8');print(f'Part {part}: {len(text)} characters => {path}')
if __name__=='__main__':main()

"""Split the whole 2026 KR Part 2 PDF (rules, guidance, appendices) into clause-item chunks.

Each chunk is one numbered item (항) of a clause (조); long items are split at
their sub-items (호), and an item that is still too long is cut into parts on
line boundaries. The clause list is checked against the PDF's own tables of
contents, so a missed or invented clause fails the run instead of silently
shrinking the corpus.

Usage:
    uv run --no-project --with pypdf==7.1.1 python data/extract_kr_part2_rules.py <pdf> <output.jsonl>
"""
import argparse
import hashlib
import json
import re
from pathlib import Path

PAGE_COUNT = 361
RULE_PAGES = range(7, 220)        # 1-based PDF pages of the rule text
GUIDE_PAGES = range(225, 361)     # guidance text and its appendices
RULE_TOC_PAGES = (4, 5, 6)
GUIDE_TOC_PAGES = (223, 224)
RULE_PAGE_OFFSET, GUIDE_PAGE_OFFSET = 5, 224
MAX_CHARS = 2400
MIN_PREAMBLE_CHARS = 300

# HancomEQN equation glyphs live in the private-use area. The mapping was read
# off the installed HancomEQN.ttf: capitals, digits, italics and symbols are
# laid out in fixed runs.
GLYPHS = {0xE000 + i: chr(ord("A") + i) for i in range(26)}
GLYPHS |= {0xE034 + i: str(i + 1) for i in range(9)} | {0xE03D: "0"}
GLYPHS |= {0xE0E5 + i: chr(ord("a") + i) for i in range(26)}
GLYPHS |= {0xE042: "%", 0xE043: "*", 0xE044: "(", 0xE045: ")", 0xE046: "-", 0xE047: "=",
           0xE048: "+", 0xE049: "[", 0xE04A: "]", 0xE04B: "{", 0xE04C: "}", 0xE052: ",",
           0xE053: ".", 0xE054: "/", 0xE05C: "√", 0xE067: "Σ", 0xE06D: "¯", 0xE09D: "α",
           0xE09E: "β", 0xE09F: "γ", 0xE0A0: "δ", 0xE0A4: "θ", 0xE0AC: "π", 0xE0AE: "σ",
           0xE0B0: "ν", 0xE0B1: "φ", 0xE0C8: "°", 0xF000: '"'}
GLYPH_TABLE = str.maketrans(GLYPHS)

CHAPTER = re.compile(r"^제 (\d+) 장  (\S.*)$")
SECTION = re.compile(r"^제 (\d+) 절  (\S.*)$")
CLAUSE = re.compile(r"^(\d)(\d{2})\.\s*(?!의)(\S.*)$")
APPENDIX = re.compile(r"^부록 (2-\d+)  (\S.*)$")
ITEM = re.compile(r"^(\d{1,2})\.\s")
SUB_ITEM = re.compile(r"^\((\d{1,2})\)\s?")
TOC_CLAUSE = re.compile(r"^(\d{3})\.\s*(.+?)\s*·+\s*\d+\s*$")
TOC_APPENDIX = re.compile(r"^부록 (2-\d+)\s+(.+?)\s*·+\s*\d+\s*$")
MARKERS = re.compile(r"\s*【(?:지침|규칙) 참조】")


def clean_title(title):
    return re.sub(r"\s+", " ", MARKERS.sub("", title)).strip()


def compact(title):
    # TOC titles and headings differ only in spacing and amendment years.
    return re.sub(r"\(\d{4}\)|\s", "", clean_title(title))


class Extractor:
    def __init__(self, pdf: Path):
        from pypdf import PdfReader  # extraction-only dependency, not a project dependency

        self.reader = PdfReader(pdf)
        cover = self.reader.pages[1].extract_text()
        if len(self.reader.pages) != PAGE_COUNT or not all(
                word in cover for word in ("2026", "재료 및 용접", "RA-02-K")):
            raise ValueError("Expected the 361-page 2026 KR Part 2 combined PDF")
        self.source_sha256 = hashlib.sha256(pdf.read_bytes()).hexdigest()

    def raw(self, number):
        return self.reader.pages[number - 1].extract_text().translate(GLYPH_TABLE)

    def body(self, number):
        lines = self.raw(number).splitlines()
        header_end = next(i for i, line in enumerate(lines[:4]) if "2026" in line)
        return [line.rstrip() for line in lines[header_end + 1:]]

    def toc(self, pages):
        clauses, appendices, chapter = set(), set(), None
        for number in pages:
            for line in self.raw(number).splitlines():
                if match := re.match(r"^제 (\d+) 장", line):
                    chapter = int(match[1])
                elif match := TOC_CLAUSE.match(line):
                    clauses.add((chapter, int(match[1]), compact(match[2])))
                elif match := TOC_APPENDIX.match(line):
                    appendices.add((match[1], compact(match[2])))
        return clauses, appendices

    def clauses(self, pages, toc, *, guidance):
        """Yield one dict per clause or appendix with its lines and their pages.

        A line opening with a clause number is a heading only when its title is
        the one the table of contents lists; otherwise it is a cross-reference
        such as "405.에 규정하는".
        """
        titles = {(chapter, clause): title for chapter, clause, title in toc}
        chapter = section = None
        current = None
        for number in pages:
            for line in self.body(number):
                if match := CHAPTER.match(line):
                    chapter = (int(match[1]), clean_title(match[2]))
                    section = None
                    continue
                if match := SECTION.match(line):
                    section = (int(match[1]), clean_title(match[2]))
                    continue
                if guidance and (match := APPENDIX.match(line)):
                    if current:
                        yield current
                    chapter = section = None
                    current = {"appendix": match[1], "title": clean_title(match[2]), "lines": []}
                    continue
                match = CLAUSE.match(line)
                title = match and titles.get((chapter and chapter[0], int(match[1] + match[2])))
                if (title and section and int(match[1]) == section[0]
                        and compact(match[3]).startswith(title)):
                    if current:
                        yield current
                    current = {"chapter": chapter[0], "chapter_title": chapter[1],
                               "section": section[0], "section_title": section[1],
                               "clause": int(match[1] + match[2]),
                               "title": clean_title(match[3]), "lines": []}
                if current is not None:
                    current["lines"].append((number, line))
        if current:
            yield current


def split_numbered(lines, pattern):
    """Split (page, line) pairs at consecutively numbered headings."""
    head, groups, expected = [], [], 1
    for page, line in lines:
        match = pattern.match(line)
        if match and int(match[1]) == expected:
            groups.append((expected, []))
            expected += 1
        (groups[-1][1] if groups else head).append((page, line))
    return head, groups


def text_of(lines):
    return "\n".join(line for _, line in lines).strip()


def cut(lines):
    """Cut an over-long run of lines into pieces under MAX_CHARS."""
    pieces, piece, size = [], [], 0
    for page, line in lines:
        if piece and size + len(line) > MAX_CHARS:
            pieces.append(piece)
            piece, size = [], 0
        piece.append((page, line))
        size += len(line) + 1
    return pieces + [piece] if piece else pieces


def heading(lines, pattern):
    """Short heading of a numbered item, e.g. "인장시험" from "4. 인장시험", or ""."""
    first = pattern.sub("", lines[0][1]).strip() if lines else ""
    head = clean_title(re.split(r"\s{2,}", first)[0])
    return head if len(head) <= 20 and not head.endswith(".") else ""


def chunks(clause_lines):
    """Return (label, heading, lines) chunks for one clause, item by item."""
    head, items = split_numbered(clause_lines, ITEM)
    if not items:
        units = [("BODY", "", clause_lines)]
    else:
        units = []
        headings = [heading(lines, ITEM) for _, lines in items]
        if len(text_of(head)) >= MIN_PREAMBLE_CHARS:
            units.append(("0", "", head))
        else:
            items[0] = (items[0][0], head + items[0][1])
        units += [(str(number), title, lines) for (number, lines), title in zip(items, headings)]
    result = []
    for label, title, lines in units:
        if len(text_of(lines)) <= MAX_CHARS:
            result.append((label, title, lines))
            continue
        lead, subs = split_numbered(lines, SUB_ITEM)
        if not subs:
            parts = [(label, title, lines)]
        else:
            parts = [(f"{label}-{number}", " ".join(filter(None, (title, heading(sub, SUB_ITEM)))), sub)
                     for number, sub in subs]
            if len(text_of(lead)) >= MIN_PREAMBLE_CHARS:
                parts.insert(0, (f"{label}-0", title, lead))
            else:
                # A bare item heading such as "4. 인장시험" is read with its first sub-item.
                parts[0] = (parts[0][0], parts[0][1], lead + parts[0][2])
        for part_label, part_title, part_lines in parts:
            pieces = cut(part_lines)
            if len(pieces) == 1:
                result.append((part_label, part_title, part_lines))
            else:
                result += [(f"{part_label}-P{i}", part_title, piece) for i, piece in enumerate(pieces, 1)]
    return [chunk for chunk in result if text_of(chunk[2])]


def extract(pdf: Path):
    extractor = Extractor(pdf)
    rows = []
    for guidance, pages, toc_pages in ((False, RULE_PAGES, RULE_TOC_PAGES),
                                       (True, GUIDE_PAGES, GUIDE_TOC_PAGES)):
        expected_clauses, expected_appendices = extractor.toc(toc_pages)
        clauses = list(extractor.clauses(pages, expected_clauses, guidance=guidance))
        found_clauses = {(c["chapter"], c["clause"], compact(c["title"])) for c in clauses if "clause" in c}
        found_appendices = {(c["appendix"], compact(c["title"])) for c in clauses if "appendix" in c}
        if found_clauses != expected_clauses or found_appendices != expected_appendices:
            raise ValueError(
                f"{'Guidance' if guidance else 'Rule'} clauses differ from the table of contents: "
                f"missing {sorted(expected_clauses - found_clauses)[:5]} "
                f"{sorted(expected_appendices - found_appendices)[:5]}, "
                f"unexpected {sorted(found_clauses - expected_clauses)[:5]} "
                f"{sorted(found_appendices - expected_appendices)[:5]}")
        offset = GUIDE_PAGE_OFFSET if guidance else RULE_PAGE_OFFSET
        source = "한국선급 선급 및 강선규칙 적용지침" if guidance else "한국선급 선급 및 강선규칙"
        for clause in clauses:
            if "appendix" in clause:
                base = f"KR-2026-2-APP-{clause['appendix']}"
                name = f"부록 {clause['appendix']} {clause['title']}"
                context = f"부록 {clause['appendix']}"
                lines = clause["lines"]
            else:
                guide = "GUIDE-" if guidance else ""
                base = f"KR-2026-2-{clause['chapter']}-{guide}{clause['clause']}"
                name = f"{'적용지침 ' if guidance else ''}{clause['clause']}. {clause['title']}"
                context = (f"제{clause['chapter']}장 {clause['chapter_title']} "
                           f"제{clause['section']}절 {clause['section_title']}")
                lines = clause["lines"]
            for label, item_title, chunk_lines in chunks(lines):
                text = text_of(chunk_lines)
                if any(0xE000 <= ord(char) <= 0xF8FF for char in text):
                    raise ValueError(f"Unmapped PDF character in {base}-{label}")
                pdf_pages = sorted({page for page, _ in chunk_lines})
                rows.append({
                    "id": f"{base}-{label}",
                    "source": source,
                    "source_sha256": extractor.source_sha256,
                    "edition": 2026,
                    "part": 2,
                    "chapter": clause.get("chapter"),
                    "section": clause.get("section"),
                    "clause": str(clause.get("clause") or f"부록 {clause['appendix']}"),
                    "item": label,
                    "title": name if label == "BODY" else f"{name} ({label}){' ' + item_title if item_title else ''}",
                    "pdf_pages": pdf_pages,
                    "printed_pages": [page - offset for page in pdf_pages],
                    "keywords": " ".join(filter(None, (context, clause["title"], item_title))),
                    "text": text,
                })
    ids = [row["id"] for row in rows]
    if len(ids) != len(set(ids)):
        duplicates = sorted({i for i in ids if ids.count(i) > 1})
        raise ValueError(f"Duplicate chunk IDs: {duplicates[:5]}")
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("pdf", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    rows = extract(args.pdf)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8", newline="\n") as output:
        for row in rows:
            output.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"{len(rows)} chunks written to {args.output}")


if __name__ == "__main__":
    main()

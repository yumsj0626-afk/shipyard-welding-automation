"""Extract the selected 2026 Korean Register clauses from the supplied PDF."""
import argparse
import hashlib
import json
from pathlib import Path

from pypdf import PdfReader


def extract(pdf: Path):
    reader = PdfReader(pdf)
    cover = reader.pages[1].extract_text()
    if len(reader.pages) != 361 or not all(word in cover for word in ("2026", "재료 및 용접", "RA-02-K")):
        raise ValueError("Expected the 361-page 2026 KR Part 2 combined PDF")
    source_hash = hashlib.sha256(pdf.read_bytes()).hexdigest()

    def page(number):
        text = reader.pages[number - 1].extract_text()
        lines = text.splitlines()
        header_end = next(i for i, line in enumerate(lines[:4]) if "2026" in line)
        body = "\n".join(line.rstrip() for line in lines[header_end + 1:])
        return body.translate(str.maketrans({"\ue035": "²", "\ue042": "%", "\ue054": "/",
                                            "\ue0e5": "a", "\uf000": '"'}))

    def between(text, start, end=None):
        offset = text.index(start)
        return text[offset:text.index(end, offset) if end else len(text)].strip()

    def entry(clause, part, title, text, pages, keywords, *, guidance=False):
        if any(0xE000 <= ord(char) <= 0xF8FF for char in text):
            raise ValueError(f"Unmapped PDF character in {clause}-{part}")
        return {
            "id": f"KR-2026-2-2-{'GUIDE-' if guidance else ''}{clause}-{part}",
            "source": "한국선급 선급 및 강선규칙 적용지침" if guidance else "한국선급 선급 및 강선규칙",
            "source_sha256": source_hash, "edition": 2026, "part": 2, "chapter": 2,
            "clause": str(clause), "title": title, "pdf_pages": pages,
            "printed_pages": [number - (224 if guidance else 5) for number in pages],
            "keywords": keywords, "text": text,
        }

    p130, p265, p152 = page(130), page(265), page(152)
    table_start = "표 2.2.3 용접용재료의 사용구분"
    table_end = "(비고)"
    notes = between(p130, table_end, "(2) 동일강도로서")
    yield entry(303, "BODY", "용접용재료의 사용구분",
                between(page(128), "303. 용접용재료") + "\n" +
                between(p130, "(2) 동일강도로서", "304. 용접준비"),
                [128, 130], "용접봉 와이어 용접재료 용접용재료 모재 강종 consumable")
    first_table = page(129).strip().replace("선\n체\n용\n압\n연\n강\n재", "선체용 압연강재")
    first_table = first_table.replace("고장력\n강", "고장력강")
    first_table = first_table.replace("RL235B, RL325A,\nRL325B", "RL235B, RL325A, RL325B")
    for part, table, number in (("TABLE-A", first_table, 129),
                                ("TABLE-B", between(p130, table_start, table_end), 130)):
        yield entry(303, part, "표 2.2.3 용접용재료의 사용구분",
                    table + "\n\n" + notes, sorted({number, 130}), "용접봉 와이어 강종 consumable")
    yield entry(304, "BODY", "용접준비",
                between(p130, "304. 용접준비", "305. 용접순서"),
                [130], "핏업 맞춤 조립 홈 가공 가용접 고정구 fitup")
    yield entry(402, "BODY", "용접절차 시방서",
                between(page(133), "402. 용접절차"),
                [133], "WPS 용접절차 시방서 용접절차서 핏업 맞춤 허용오차")

    start407 = between(page(148), "407. 승인된")
    base = "(1) 모재  모재의"
    thickness = "(2) 모재두께 및 바깥지름"
    position = "(3) 용접자세"
    method = "(4) 용접법"
    yield entry(407, "GENERAL", "승인된 용접절차 시방서의 허용범위",
                start407[:start407.index(base)].strip(), [148], "WPS 재승인 필수변수 허용범위")
    p149 = page(149)
    yield entry(407, "BASE", "407.2(1) 모재",
                between(start407, base) + "다.\n" + between(p149, "(c) (a) 및 (b)", thickness),
                [148, 149], "모재 강종 강도 인성 열처리")
    p151 = page(151)
    yield entry(407, "THICKNESS", "407.2(2) 모재두께 및 바깥지름",
                "\n".join((between(p149, thickness), page(150).strip(), p151[:p151.index(position)].strip())),
                [149, 150, 151], "두께 판 관 바깥지름 목두께")
    yield entry(407, "POSITION", "407.2(3) 용접자세",
                between(p151, position, method), [151], "자세 수직상진 수직하진 필릿")
    yield entry(407, "METHOD", "407.2(4) 용접법",
                between(p151, method) + "\n" + p152[:p152.index("(5) 용접용재료")].strip(),
                [151, 152], "용접법 용접공정 다층 일층")
    parts = [
        ("CONSUMABLE", "(5) 용접용재료", "(6) 용접조건", "용접봉 와이어 상표 보호가스 용접용재료"),
        ("CONDITIONS", "(6) 용접조건", "(7) 용접입열량", "전류 극성 예열 층간온도 후열처리"),
        ("HEAT", "(7) 용접입열량", "(8) 이음의 형태", "입열 열량"),
        ("JOINT", "(8) 이음의 형태", "(9) 기타", "핏업 맞춤 루트 간격 루트면 홈 허용오차 fitup"),
        ("OTHER", "(9) 기타", None, "재승인 변경"),
    ]
    for part, start, end, keywords in parts:
        yield entry(407, part, f"407.2{start}", between(p152, start, end), [152], keywords)
    yield entry(303, "BODY", "적용지침 303. 용접용재료의 사용구분",
                between(p265, "303. 용접용재료", "304. 용접준비"), [265],
                "수소균열 시험", guidance=True)
    yield entry(304, "BODY", "적용지침 304. 용접준비",
                between(p265, "304. 용접준비", "305. 용접순서"), [265],
                "가용접 비드 길이 피치", guidance=True)
    yield entry(407, "BODY", "적용지침 407. 용접 허용범위",
                between(page(271), "407. 승인된"), [271],
                "허용오차 홈 루트 간격 선급 인정", guidance=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    rows = list(extract(args.pdf))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8", newline="\n") as output:
        for row in rows:
            output.write(json.dumps(row, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()

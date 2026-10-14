"""A PDF whose pages are the rows of a table, so a reader can recover the original data."""
from __future__ import annotations

from typing import List

import pandas as pd


def table_pdf(frame: pd.DataFrame) -> bytes:
    """The rows of `frame` as a PDF. Each line is one CSV row, so the text is the table."""
    lines = frame.to_csv(index=False).splitlines()
    return _lines_pdf(lines)


def _lines_pdf(lines: List[str]) -> bytes:
    per_page = 55
    chunks = [lines[i:i + per_page] for i in range(0, len(lines), per_page)] or [[]]
    font_id = 3 + 2 * len(chunks)  # catalog, pages, then page + stream pairs, then the font
    objects: List[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"",  # pages object, filled once the kids are known
    ]
    kids = []
    for n, chunk in enumerate(chunks):
        page_id = 3 + 2 * n
        stream_id = page_id + 1
        kids.append(f"{page_id} 0 R".encode())
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {stream_id} 0 R "
            f"/Resources << /Font << /F1 {font_id} 0 R >> >> >>".encode()
        )
        stream = _page_stream(chunk)
        objects.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Courier >>")
    objects[1] = b"<< /Type /Pages /Kids [" + b" ".join(kids) + f"] /Count {len(chunks)} >>".encode()
    return _assemble(objects)


def _page_stream(lines: List[str]) -> bytes:
    commands = ["BT", "/F1 9 Tf", "36 760 Td"]
    for n, line in enumerate(lines):
        escaped = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        move = "" if n == 0 else "0 -12 Td "
        commands.append(f"{move}({escaped}) Tj")
    commands.append("ET")
    return "\n".join(commands).encode()


def _assemble(objects: List[bytes]) -> bytes:
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for n, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{n} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)

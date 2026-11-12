import argparse
import json
from pathlib import Path
from tempfile import TemporaryDirectory

import pymupdf


def main():
    parser = argparse.ArgumentParser(description="按页提取论文 PDF 正文")
    parser.add_argument("pdf", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()

    args.output.parent.mkdir(parents=True, exist_ok=True)
    pages_with_text = 0

    with TemporaryDirectory(dir=args.output.parent) as temp_dir:
        temp_path = Path(temp_dir) / args.output.name
        with pymupdf.open(args.pdf) as document, temp_path.open(
            "w", encoding="utf-8"
        ) as output:
            if not document.is_pdf or document.needs_pass:
                raise SystemExit("输入文件必须是可读的 PDF。")

            page_count = document.page_count
            for page_number, page in enumerate(document, start=1):
                text = page.get_text("text", sort=True).strip()
                pages_with_text += bool(text)
                record = {
                    "paper_id": args.pdf.stem,
                    "page": page_number,
                    "text": text,
                }
                print(json.dumps(record, ensure_ascii=False), file=output)

        if pages_with_text == 0:
            raise SystemExit("未提取到正文，请检查 PDF 文本层。")

        temp_path.replace(args.output)

    print(f"总页数 {page_count}，有文本的页数 {pages_with_text}")
    print(f"输出文件 {args.output}")


if __name__ == "__main__":
    main()

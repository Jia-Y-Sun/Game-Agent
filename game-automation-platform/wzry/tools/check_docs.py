"""检查所有 Markdown 文档的本地链接、代码块围栏与表格格式。

用法：

```bat
python tools/check_docs.py
python tools/check_docs.py --root . --verbose
```

退出码非 0 表示有问题，可直接放进 CI。
"""

import argparse
import os
import re
import sys
from urllib.parse import unquote

LINK_RE = re.compile(r"!?\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
FENCE_RE = re.compile(r"^\s*(```+|~~~+)")
TABLE_SEP_RE = re.compile(r"^\|[\s:\-\|]+\|?\s*$")
HTML_ANCHOR_RE = re.compile(r"^<a\s+name=")


def iter_markdown(root):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            name for name in dirnames
            if name not in {".git", "__pycache__", ".venv", "venv", "human_data", "scrcpy-win64-v2.0"}
        ]
        for name in sorted(filenames):
            if name.lower().endswith(".md"):
                yield os.path.join(dirpath, name)


def check_file(path, root, verbose=False):
    problems = []
    with open(path, "r", encoding="utf-8", errors="replace") as file:
        text = file.read()
    lines = text.splitlines()

    # 1) 代码块围栏成对
    fences = [index + 1 for index, line in enumerate(lines) if FENCE_RE.match(line)]
    if len(fences) % 2 != 0:
        problems.append(f"代码块围栏数量为奇数（{len(fences)} 个），最后一个在第 {fences[-1]} 行")

    # 2) 表格：连续的 | 行块，第二行必须是分隔行
    block = []
    for index, line in enumerate(lines + [""]):
        if line.strip().startswith("|"):
            block.append((index + 1, line))
            continue
        if block:
            if len(block) >= 2 and not TABLE_SEP_RE.match(block[1][1].strip()):
                problems.append(f"第 {block[0][0]} 行起的表格缺少分隔行（|---|）")
            block = []

    # 3) 本地链接与图片
    base = os.path.dirname(os.path.abspath(path))
    for match in LINK_RE.finditer(text):
        label, target = match.group(1), match.group(2)
        if target.startswith(("http://", "https://", "mailto:", "#")):
            continue
        pure = target.split("#", 1)[0]
        if not pure:
            continue
        pure = unquote(pure)
        candidate = os.path.normpath(os.path.join(base, pure))
        if not os.path.exists(candidate):
            line_no = text[: match.start()].count("\n") + 1
            problems.append(f"第 {line_no} 行链接失效: [{label}]({target})")

    if verbose and not problems:
        print(f"  ok  {os.path.relpath(path, root)}")
    return problems


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    root = os.path.abspath(args.root)
    files = list(iter_markdown(root))
    if not files:
        print("没有找到任何 .md 文件")
        return 1

    total_problems = 0
    for path in files:
        problems = check_file(path, root, verbose=args.verbose)
        if problems:
            total_problems += len(problems)
            print(f"[FAIL] {os.path.relpath(path, root)}")
            for item in problems:
                print(f"       - {item}")

    print(f"\n检查 {len(files)} 个 Markdown 文件，问题 {total_problems} 处")
    return 0 if total_problems == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

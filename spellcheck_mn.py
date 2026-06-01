#!/usr/bin/env python3
"""
spellcheck.mn (Болорспелл) API ашиглан LaTeX файлд Монгол алдаа шалгагч.

Хэрэглээ:
    # Шалгаад зөвхөн тайлан хэвлэх (засахгүй):
    python3 spellcheck_mn.py Chapters/*.tex

    # Дотроос автоматаар засах (interactive асуулгатай):
    python3 spellcheck_mn.py --fix Chapters/*.tex

    # Бүх .tex файлыг шалгах:
    python3 spellcheck_mn.py --all

    # Тодорхой алдаатай үг + засваруудыг (.json) ашиглан автомат засах:
    python3 spellcheck_mn.py --apply fixes.json Chapters/*.tex

API анализ:
- POST https://spellcheck.mn/cms-client/modules/spellchecker/check
    body: {"text": "<монгол текст>", "key": <encrypt(text)>}
    хариу: ["алдаатай_үг1", "алдаатай_үг2", ...]
- POST .../suggest body: {"word": "<үг>", "key": encrypt(word)}  -> ["санал1", ...]
- key = SHA256(  sum((codepoint+1) for char in text) mod (10^10+8)  )
"""
from __future__ import annotations
import argparse, hashlib, json, re, sys, time
import urllib.request, urllib.error
from collections import defaultdict
from pathlib import Path

API_BASE = "https://spellcheck.mn/cms-client/modules/spellchecker"
HEADERS = {
    "Content-Type": "application/json",
    "Origin": "https://spellcheck.mn",
    "Referer": "https://spellcheck.mn/",
    "User-Agent": "Mozilla/5.0",
}
CHUNK_LIMIT = 700 
SKIP_ENVS = {"lstlisting", "verbatim", "tikzpicture", "tabular", "equation", "align", "math"}


def encrypt_key(text: str) -> str:
    """Эх кодын `$encrypt(text)`-ийн Python хувилбар.
    Болорспеллийн frontend bundle-аас гаргаж авав:
      1. n = Σ(codepoint(ch) + 1)  mod (10^10 + 8)
      2. key = SHA256(str(n))
    """
    modulus = 10**10 + 8
    n = 0
    for ch in text:
        n = (n + ord(ch) + 1) % modulus
    return hashlib.sha256(str(n).encode("utf-8")).hexdigest()


def _api(endpoint: str, payload: dict, retries: int = 3, timeout: int = 30):
    body = json.dumps(payload).encode("utf-8")
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(f"{API_BASE}/{endpoint}", data=body, headers=HEADERS, method="POST")
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 400 or attempt == retries:
                raise
            time.sleep(0.6)
        except Exception:
            if attempt == retries:
                raise
            time.sleep(0.6)


def check_text(text: str):
    return _api("check", {"text": text, "key": encrypt_key(text)})


def suggest_word(word: str):
    return _api("suggest", {"word": word, "key": encrypt_key(word)})


# --------- LaTeX ялгах ---------
def latex_to_text_lines(src: str):
    """LaTeX-аас зөвхөн монгол үгсийг (мөрийн дугаарт холбож) гаргана.
    Дагавар хуурамч-эерэг (suffix false-positive)-ыг багасгахын тулд:
      - Latin/digit ба зураас бүхий газруудыг хоосон болгож үг таслана
      - 3 ба түүнээс дээш тэмдэгттэй кириллик үгсийг л авна
    """
    lines = src.splitlines()
    out = []
    in_skip = None
    for i, raw in enumerate(lines, 1):
        line = raw
        if in_skip:
            if re.search(r"\\end\{" + re.escape(in_skip) + r"\}", line):
                in_skip = None
            continue
        m = re.search(r"\\begin\{([a-zA-Z*]+)\}", line)
        if m and m.group(1).rstrip("*") in SKIP_ENVS:
            in_skip = m.group(1)
            continue
        # comments
        line = re.sub(r"(?<!\\)%.*$", "", line)
        # math
        line = re.sub(r"\$[^$]*\$", " ", line)
        # url/href/cite/label/ref/texttt — текстийг шалгахгүй
        line = re.sub(
            r"\\(cite|label|ref|input|include|includegraphics|url|href|texttt|lstinline|hypertarget|hyperref)\s*(\[[^\]]*\])?\{[^{}]*\}",
            " ", line,
        )
        # styling — кирилл текстийг үлдээж зөвхөн команд тэмдгийг хасах
        for cmd in (
            "textbf textit emph underline section subsection subsubsection "
            "chapter caption textcolor footnote textnormal textsc"
        ).split():
            line = re.sub(r"\\" + cmd + r"\s*(\[[^\]]*\])?\{", " ", line)
        # бусад LaTeX команд
        line = re.sub(r"\\[a-zA-Z]+\*?\s*(\[[^\]]*\])?", " ", line)
        line = line.replace("{", " ").replace("}", " ").replace("\\\\", " ").replace("&", " ")
        # Latin/тоо/хальт-аас үг тусгаарлах, цэвэр кирилл л үлдээх
        cleaned = re.sub(r"[^\sЀ-ӿ]", " ", line)
        words = re.findall(r"[Ѐ-ӿ]{3,}", cleaned)
        if words:
            out.append((i, " ".join(words)))
    return out

def scan_file(path: Path):
    src = path.read_text(encoding="utf-8")
    per_line = latex_to_text_lines(src)
    word_lines: dict[str, set[int]] = defaultdict(set)
    for ln, txt in per_line:
        for w in txt.split():
            word_lines[w].add(ln)

    chunks, buf, blen = [], [], 0
    for ln, txt in per_line:
        if blen + len(txt) > CHUNK_LIMIT and buf:
            chunks.append(buf)
            buf, blen = [], 0
        buf.append((ln, txt))
        blen += len(txt) + 1
    if buf:
        chunks.append(buf)

    errors = set()
    for c in chunks:
        text = " ".join(t for _, t in c)
        try:
            for w in check_text(text):
                errors.add(w)
        except Exception as e:
            print(f"  [алдаа] {path.name}: чанк илгээгдсэнгүй ({e})", file=sys.stderr)
    return errors, word_lines


def filter_real_issues(errors: set[str]) -> tuple[list[str], list[str]]:
    """Богино дагавар хуурамч-эерэг үгсийг тусгаарлах."""
    SHORT_SUFFIXES = {"аас", "оос", "ыг", "ийг", "ын", "ийн", "ууд", "үүд", "тэй", "тай", "той", "тэй"}
    real, suffix = [], []
    for w in sorted(errors):
        if len(w) <= 3 or w in SHORT_SUFFIXES:
            suffix.append(w)
        else:
            real.append(w)
    return real, suffix


# --------- Тайлан + засах ---------
def print_report(path: Path, errors: set[str], word_lines: dict[str, set[int]], show_suggest: bool):
    real, suffix = filter_real_issues(errors)
    print(f"\n========== {path} ==========")
    print(f"  нийт алдаа: {len(errors)}  (бодит: {len(real)}, дагавар хуурамч-эерэг: {len(suffix)})")
    if not errors:
        return
    print("\n  Бодит шалгах ёстой үгс:")
    for w in sorted(real, key=lambda w: min(word_lines[w])):
        lines = sorted(word_lines[w])
        ls = ",".join(str(l) for l in lines[:6]) + ("…" if len(lines) > 6 else "")
        sugg = ""
        if show_suggest:
            try:
                sugg = "  → " + ", ".join(suggest_word(w)[:4])
            except Exception:
                sugg = ""
        print(f"    L{ls:<22} {w}{sugg}")
    if suffix:
        print(f"\n  Дагавар (магадгүй LaTeX-аас үлдсэн): {', '.join(suffix)}")


def apply_fixes(path: Path, fixes: dict[str, str]) -> int:
    """fixes: {"буруу": "зөв"}. Зөвхөн яг ижил үг солих."""
    src = path.read_text(encoding="utf-8")
    new = src
    applied = 0
    for bad, good in fixes.items():
        if bad.startswith("_"):  # _comment, _section гэх мэт түлхүүрийг алгасах
            continue
        # Үг хүрээтэй (whitespace эсвэл цэг таслал) солилт хийнэ
        pat = re.compile(r"(?<![Ѐ-ӿ])" + re.escape(bad) + r"(?![Ѐ-ӿ])")
        new2, count = pat.subn(good, new)
        if count:
            applied += count
            new = new2
            print(f"  [{path.name}] {bad} → {good}  ({count} газар)")
    if applied:
        path.write_text(new, encoding="utf-8")
    return applied


def interactive_fix(path: Path, errors: set[str], word_lines: dict[str, set[int]]):
    real, _ = filter_real_issues(errors)
    fixes: dict[str, str] = {}
    for w in sorted(real, key=lambda w: min(word_lines[w])):
        try:
            sugg = suggest_word(w)[:5]
        except Exception:
            sugg = []
        if not sugg:
            print(f"  {w}: санал байхгүй → алгасъя")
            continue
        print(f"\n  «{w}» (мөр: {sorted(word_lines[w])[:5]})")
        for i, s in enumerate(sugg, 1):
            print(f"    {i}. {s}")
        print("    0. алгасах   c. өөрөө бичих")
        ans = input("    сонгох: ").strip()
        if ans == "c":
            new = input("    шинэ үг: ").strip()
            if new:
                fixes[w] = new
        elif ans.isdigit() and 1 <= int(ans) <= len(sugg):
            fixes[w] = sugg[int(ans) - 1]
    if fixes:
        apply_fixes(path, fixes)


# --------- CLI ---------
def main():
    ap = argparse.ArgumentParser(description="LaTeX файлд Болорспелл алдаа шалгагч")
    ap.add_argument("paths", nargs="*", help=".tex файлууд")
    ap.add_argument("--all", action="store_true", help="Chapters/, FrontBackMatter/, Appendices/-ийн бүх .tex файлыг авах")
    ap.add_argument("--suggest", action="store_true", help="Алдаа бүрд саналыг харуулах")
    ap.add_argument("--fix", action="store_true", help="Интерактив горимоор засах")
    ap.add_argument("--apply", metavar="JSON", help='Өгсөн JSON-оор автомат засах. Жнь: {"буруу": "зөв"}')
    args = ap.parse_args()

    root = Path(__file__).parent
    if args.all:
        paths = sorted(root.glob("Chapters/*.tex")) + sorted(root.glob("FrontBackMatter/*.tex")) + sorted(root.glob("Appendices/*.tex"))
        paths = [p for p in paths if p.name not in ("bib.tex",)]
    else:
        paths = [Path(p) for p in args.paths]

    if not paths:
        ap.error("Файл өгөөгүй байна. --all эсвэл файлын замыг өгнө үү.")

    fixes_global: dict[str, str] | None = None
    if args.apply:
        fixes_global = json.loads(Path(args.apply).read_text(encoding="utf-8"))

    summary = []
    for p in paths:
        if not p.exists():
            print(f"  алгасав: {p} (олдсонгүй)", file=sys.stderr)
            continue
        if fixes_global is not None:
            n = apply_fixes(p, fixes_global)
            summary.append((p, n))
            continue
        errs, wl = scan_file(p)
        print_report(p, errs, wl, show_suggest=args.suggest)
        if args.fix and errs:
            interactive_fix(p, errs, wl)
        summary.append((p, len(errs)))

    print("\n===== ХУРААНГУЙ =====")
    label = "засагдсан үгс" if fixes_global else "нийт алдаа"
    for p, n in summary:
        print(f"  {p}: {n} {label}")


if __name__ == "__main__":
    main()

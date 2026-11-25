"""Estää henkilötietojen päätymisen julkiseen repoon (ajetaan CI:ssä).

Tarkistaa tekstitiedostoista:
* oikean näköiset VIN-numerot (17 merkkiä; testien synteettisissä on sana TEST);
* tarkat koordinaatit Suomen alueella (vähintään 5 desimaalia; testidata on siirretty pois);
* sähköpostiosoitteet (paitsi noreply- ja example-osoitteet).

Käyttö: python tools/privacy_check.py [kansio]
"""
import re
import sys
from pathlib import Path

ROOT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[1]
SKIP_DIRS = {".git", "__pycache__", ".pytest_cache"}
TEXT = {".py", ".md", ".yaml", ".yml", ".html", ".js", ".css", ".txt", ".json", ".sh", ".toml", ".cfg", ""}

VIN = re.compile(r"\b(?=[A-HJ-NPR-Z0-9]*[A-Z])(?=[A-HJ-NPR-Z0-9]*\d)[A-HJ-NPR-Z0-9]{17}\b")
NUM = re.compile(r"(?<![\d.])(\d{2})\.(\d{5,})(?![\d.])")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")


def check(path: Path) -> list[str]:
    out = []
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return out
    for no, line in enumerate(text.splitlines(), 1):
        for m in VIN.finditer(line):
            if "TEST" not in m.group(0):
                out.append(f"{path}:{no}: VIN-numerolta näyttävä merkkijono")
        nums = [(int(a), m) for m in NUM.finditer(line) for a in [m.group(1)]]
        if any(59 <= v <= 70 for v, _ in nums) and any(19 <= v <= 31 for v, _ in nums):
            out.append(f"{path}:{no}: tarkka koordinaatti Suomen alueella")
        for m in EMAIL.finditer(line):
            e = m.group(0).lower()
            if not (e.startswith("noreply@") or "example." in e or e.endswith("@users.noreply.github.com")):
                out.append(f"{path}:{no}: sähköpostiosoite")
    return out


def main() -> int:
    problems = []
    for p in ROOT.rglob("*"):
        if p.is_file() and not (set(p.relative_to(ROOT).parts) & SKIP_DIRS) and p.suffix in TEXT and p.name != "privacy_check.py":
            problems += check(p)
    for line in problems:
        print(line)
    if problems:
        print(f"{len(problems)} mahdollista henkilötietoa. Poista ne tai korvaa synteettisellä datalla.")
        return 1
    print("Henkilötietotarkistus OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())

import io
import re
import tokenize
from dataclasses import dataclass, field
from pathlib import Path

PLAYBOOK_PATH = Path(__file__).resolve().parents[1] / "PLAYBOOK.md"

PATTERN_HEADING = re.compile(r"^## (\d+)\. (.+)$")
SUBSECTION_HEADING = re.compile(r"^### (.+)$")
FENCE = re.compile(r"^```(\w+)?\s*$")

REQUIRED_SUBSECTIONS = ("The burn", "Sketch", "The judgment call")
PERCENT = re.compile(r"\b\d+(?:\.\d+)?\s?%")
PERCENT_WORD = re.compile(r"\b\d+(?:\.\d+)?\s?percent\b", re.IGNORECASE)
MULTIPLIER = re.compile(r"\b\d+(?:\.\d+)?x\b")
CURRENCY = re.compile(r"[$€£]\s?\d[\d,]*(?:\.\d+)?\s?[kKmMbB]?\b")


@dataclass
class CodeBlock:
    lang: str
    source: str


@dataclass
class Pattern:
    number: int
    title: str
    subsections: list[str] = field(default_factory=list)
    code_blocks: list[CodeBlock] = field(default_factory=list)
    prose: str = ""

    @property
    def use_headings(self) -> list[str]:
        return [s for s in self.subsections if s.lower().startswith("use")]

    @property
    def decision_headings(self) -> list[str]:
        return [
            s
            for s in self.subsections
            if s.lower().startswith("use") or s.lower().startswith("skip")
        ]

    @property
    def python_blocks(self) -> list[CodeBlock]:
        return [b for b in self.code_blocks if b.lang == "python"]


@dataclass
class Playbook:
    patterns: list[Pattern]
    prose: str


def load_playbook(path: Path = PLAYBOOK_PATH) -> Playbook:
    lines = path.read_text(encoding="utf-8").splitlines()
    patterns: list[Pattern] = []
    current: Pattern | None = None
    current_subsection: str | None = None
    in_fence = False
    fence_lang = ""
    fence_buffer: list[str] = []
    prose_lines: list[str] = []
    pattern_prose: list[str] = []

    def flush_pattern() -> None:
        if current is not None:
            current.prose = "\n".join(pattern_prose)

    for line in lines:
        fence_match = FENCE.match(line)
        if fence_match and not in_fence:
            in_fence = True
            fence_lang = fence_match.group(1) or ""
            fence_buffer = []
            continue
        if in_fence:
            if line.strip() == "```":
                in_fence = False
                if current is not None:
                    current.code_blocks.append(
                        CodeBlock(lang=fence_lang, source="\n".join(fence_buffer))
                    )
                continue
            fence_buffer.append(line)
            continue

        pattern_match = PATTERN_HEADING.match(line)
        if pattern_match:
            flush_pattern()
            current = Pattern(
                number=int(pattern_match.group(1)), title=pattern_match.group(2).strip()
            )
            patterns.append(current)
            pattern_prose = []
            current_subsection = None
            continue

        sub_match = SUBSECTION_HEADING.match(line)
        if sub_match and current is not None:
            current_subsection = sub_match.group(1).strip()
            current.subsections.append(current_subsection)
            continue

        prose_lines.append(line)
        if current is not None:
            pattern_prose.append(line)

    flush_pattern()
    return Playbook(patterns=patterns, prose="\n".join(prose_lines))


def parse_error(source: str) -> str | None:
    import ast

    try:
        ast.parse(source)
    except SyntaxError as exc:
        return f"{exc.msg} (line {exc.lineno})"
    return None


def comment_lines(source: str) -> list[int]:
    found: list[int] = []
    reader = io.StringIO(source + "\n").readline
    try:
        for tok in tokenize.generate_tokens(reader):
            if tok.type == tokenize.COMMENT:
                found.append(tok.start[0])
    except tokenize.TokenError:
        return found
    return found


def metric_hits(text: str) -> list[str]:
    hits: list[str] = []
    for rx in (PERCENT, PERCENT_WORD, MULTIPLIER, CURRENCY):
        hits.extend(rx.findall(text))
    return hits

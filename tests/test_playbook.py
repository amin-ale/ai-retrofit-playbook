import pytest

from playbook_lint import (
    REQUIRED_SUBSECTIONS,
    comment_lines,
    load_playbook,
    metric_hits,
    parse_error,
)

playbook = load_playbook()


def test_playbook_has_seven_patterns():
    numbers = [p.number for p in playbook.patterns]
    assert numbers == list(range(1, 8))


@pytest.mark.parametrize("pattern", playbook.patterns, ids=lambda p: p.title)
def test_pattern_has_required_subsections(pattern):
    missing = [s for s in REQUIRED_SUBSECTIONS if s not in pattern.subsections]
    assert not missing, f"{pattern.title} missing {missing}"


@pytest.mark.parametrize("pattern", playbook.patterns, ids=lambda p: p.title)
def test_pattern_frames_when_to_use(pattern):
    assert pattern.use_headings, f"{pattern.title} has no 'Use ... when' section"


@pytest.mark.parametrize("pattern", playbook.patterns, ids=lambda p: p.title)
def test_pattern_frames_both_sides_of_decision(pattern):
    assert len(pattern.decision_headings) >= 2, (
        f"{pattern.title} does not frame when-to and when-not-to"
    )


@pytest.mark.parametrize("pattern", playbook.patterns, ids=lambda p: p.title)
def test_pattern_has_python_sketch(pattern):
    assert pattern.python_blocks, f"{pattern.title} has no python code sketch"


@pytest.mark.parametrize("pattern", playbook.patterns, ids=lambda p: p.title)
def test_pattern_sketches_parse(pattern):
    for block in pattern.python_blocks:
        error = parse_error(block.source)
        assert error is None, f"{pattern.title} sketch does not parse: {error}"


@pytest.mark.parametrize("pattern", playbook.patterns, ids=lambda p: p.title)
def test_pattern_sketches_are_comment_free(pattern):
    for block in pattern.python_blocks:
        lines = comment_lines(block.source)
        assert not lines, f"{pattern.title} sketch has comments on lines {lines}"


def test_prose_makes_no_fabricated_metric_claims():
    hits = metric_hits(playbook.prose)
    assert not hits, f"prose contains measurement-shaped numbers: {hits}"


@pytest.mark.parametrize(
    "sample",
    [
        "cut the bill by 40%",
        "throughput jumped 3x",
        "we saved $50k a month",
        "down to €1.5M in spend",
        "42 percent cheaper",
    ],
)
def test_metric_lint_catches_fabricated_shapes(sample):
    assert metric_hits(sample), f"lint missed metric shape in: {sample!r}"

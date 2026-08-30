"""Synthetic and local-source tests for the OALD10 parser."""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from mdict_utils.base.readmdict import MDX

from oald10_easydict_service.models import Sense
from oald10_easydict_service.parser import parse_record

if TYPE_CHECKING:
    from oald10_easydict_service.models import Entry, SenseNode

FIXTURE = Path(__file__).parent / "fixtures" / "synthetic_entry.html"
LOCAL_MDX = Path("sources/oald10-bilingual/oald10-bilingual.mdx")


def _as_sense(node: SenseNode) -> Sense:
    assert isinstance(node, Sense)
    return node


def test_synthetic_entry_preserves_examples_and_structure() -> None:
    parsed = parse_record(FIXTURE.read_text(encoding="utf-8"), 7, "sample")

    assert [entry.pos for entry in parsed.entries] == ["noun", "verb"]
    noun, verb = parsed.entries
    assert noun.entry_id == (7 << 10) | 1
    assert verb.entry_id == (7 << 10) | 2
    assert noun.headword == "sample"
    assert [(item.region, item.notation) for item in noun.pronunciation] == [
        ("UK", "/naʊn/"),
        ("US", "/naʊn-us/"),
    ]
    assert [(item.region, item.notation) for item in verb.pronunciation] == [
        ("UK", "/vɜːb/"),
        ("US", "/vɜːrb/"),
    ]
    first_sense = noun.sense[0]
    assert first_sense.kind == "sense"
    assert first_sense.labels.grammar == ["countable"]
    assert first_sense.labels.register_labels == ["informal"]
    assert first_sense.labels.region == ["BrE"]
    assert first_sense.labels.usage == ["sometimes humorous"]
    assert first_sense.labels.construction == ["sample of sth"]
    assert [(item.en, item.zh) for item in first_sense.examples] == [
        ("A sample answer.", "一个示例答案。"),
        ("Second example.", "第二个例句。"),
    ]
    group = noun.sense[1]
    assert group.kind == "group"
    assert group.title.en == "CATEGORY"
    assert group.senses[0].kind == "sense"
    assert group.senses[0].examples[0].en == "Grouped example."
    assert len(noun.child_idioms) == 1
    assert noun.child_idioms[0].headword == "make a sample"
    assert _as_sense(noun.child_idioms[0].sense[0]).examples[0].en == "Idiom example."
    assert _as_sense(noun.child_phrasal_verbs[0].sense[0]).examples[0].en == "Phrasal-verb example."
    derivative = noun.child_derivatives[0]
    assert derivative.headword == "sampler"
    assert derivative.cross_references[0].target == "sampler"
    derivative_sense = _as_sense(derivative.sense[0])
    assert derivative_sense.definition is None
    assert derivative_sense.examples[0].en == "Derivative example."
    assert verb.sense[0].kind == "sense"
    assert verb.sense[0].definition is None
    assert verb.sense[0].cross_references[0].target == "try"
    assert verb.sense[0].examples[0].en == "Reference example."
    serialized = json.dumps(
        parsed.model_dump(mode="json", by_alias=True), ensure_ascii=False
    ).casefold()
    assert "do not keep" not in serialized
    assert "excluded boxed example" not in serialized
    assert "excluded duplicate example" not in serialized
    assert "audio/example" not in serialized
    assert "do not keep audio text" not in serialized
    assert "boxed__gb_1.mp3" in parsed.metrics.audio_references
    assert "audio/example/no.mp3" not in parsed.metrics.audio_references
    assert parsed.metrics.english_only_definitions == 0
    assert parsed.metrics.chinese_only_definitions == 0
    assert parsed.metrics.raw_examples == 9
    assert parsed.metrics.retained_examples == 7
    assert parsed.metrics.retained_example_lists == 6
    assert parsed.metrics.excluded_examples == 2
    assert parsed.metrics.missing_english_examples == 0
    assert parsed.metrics.missing_chinese_examples == 0
    assert parsed.metrics.orphan_examples == 0
    assert parsed.metrics.duplicate_example_imports == 0


@pytest.mark.skipif(not LOCAL_MDX.exists(), reason="local copyrighted source is absent")
def test_local_source_acceptance_samples() -> None:
    mdx = MDX(str(LOCAL_MDX))
    wanted = {
        "answer",
        "run",
        "record",
        "information",
        "the",
        "good",
        "set",
        "read",
        "answer back",
        "abbreviate",
        "ad",
        "aren’t",
    }
    parsed: dict[str, list[Entry]] = defaultdict(list)
    for number, (raw_key, value) in enumerate(mdx.items(), start=1):
        key = raw_key.decode("utf-8")
        if key not in wanted or value.lstrip().startswith(b"@@@LINK="):
            continue
        parsed[key].extend(parse_record(value, number, key).entries)

    assert set(parsed) == wanted
    answer = parsed["answer"]
    assert [len(entry.sense) for entry in answer] == [4, 3]
    record = parsed["record"]
    assert [[item.notation for item in entry.pronunciation] for entry in record] == [
        ["/ˈrekɔːd/", "/ˈrekərd/"],
        ["/rɪˈkɔːd/", "/rɪˈkɔːrd/"],
    ]
    assert parsed["answer back"][0].pos == "phrasal verb"
    assert _as_sense(parsed["answer back"][0].sense[0]).examples
    assert _as_sense(answer[0].sense[0]).examples[0].en == (
        "I rang the bell, but there was no answer."
    )
    assert _as_sense(answer[1].sense[0]).examples[0].en == (
        "I repeated the question, but she didn't answer."
    )
    abbreviated = _as_sense(parsed["abbreviate"][0].child_derivatives[0].sense[0])
    assert abbreviated.definition is None
    assert abbreviated.examples[0].en == "Where appropriate, abbreviated forms are used."
    ad_sense = parsed["ad"][0].sense[0]
    assert ad_sense.kind == "sense"
    assert ad_sense.definition is None
    assert ad_sense.cross_references[0].target == "advertisement"
    assert ad_sense.examples[0].en == "The TV ads were first run last year."
    contraction_sense = parsed["aren’t"][0].sense[1]
    assert contraction_sense.kind == "sense"
    assert contraction_sense.definition is None
    assert contraction_sense.cross_references[0].target == "am not"
    assert contraction_sense.examples[0].en == "Aren't I clever?"
    serialized = json.dumps(
        [
            entry.model_dump(mode="json", by_alias=True)
            for values in parsed.values()
            for entry in values
        ],
        ensure_ascii=False,
    ).casefold()
    assert '"examples"' in serialized
    assert "audio/example" not in serialized

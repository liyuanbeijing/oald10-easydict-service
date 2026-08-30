"""Purpose-built parser for OALD10 bilingual HTML records."""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import PurePosixPath
from urllib.parse import quote, unquote, urlparse

from bs4 import BeautifulSoup, Tag

from oald10_easydict_service.models import (
    ChildEntry,
    CrossReference,
    Definition,
    Entry,
    Example,
    Labels,
    ParsedRecord,
    ParseMetrics,
    Pronunciation,
    Sense,
    SenseGroup,
    SenseNode,
)

_STRESS_MARKS = str.maketrans("", "", "·ˈˌ")
_SPACE_RE = re.compile(r"\s+")
MAX_LOCAL_ENTRIES = 1023
_REMOVED_SELECTORS = (
    ".o-unbox-panel",
    ".o-symbol-wrap",
)
_EXCLUDED_COLLECTION_CLASSES = {
    "o-phrase-collection",
    "o-idiom-collection",
    "o-phrasal-collection",
}


class ParseError(ValueError):
    """Raised when a non-redirect MDX record cannot be classified safely."""


def _classes(node: Tag) -> set[str]:
    value = node.get("class")
    if value is None:
        return set()
    if isinstance(value, str):
        return {value}
    return {str(item) for item in value}


def clean_text(value: str) -> str:
    """Collapse HTML whitespace without altering Unicode content."""

    return _SPACE_RE.sub(" ", value).strip()


def clean_headword(value: str) -> str:
    """Remove OALD syllable and stress marks from an indexable headword."""

    return clean_text(value.translate(_STRESS_MARKS))


def _unique(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = clean_text(value.strip(" ,;()[]"))
        folded = cleaned.casefold()
        if cleaned and folded not in seen:
            seen.add(folded)
            result.append(cleaned)
    return result


def _audio_filename(href: str) -> str | None:
    prefix = "sound://audio/word/"
    if not href.casefold().startswith(prefix):
        return None
    filename = unquote(href[len(prefix) :])
    if PurePosixPath(filename).name != filename or not filename.casefold().endswith(".mp3"):
        raise ParseError(f"unsafe word audio reference: {href!r}")
    return filename


def _parse_pronunciations(node: Tag, metrics: ParseMetrics) -> list[Pronunciation]:
    pronunciations: list[Pronunciation] = []
    seen: set[tuple[str, str, str | None]] = set()
    chunks = [node] if "o-pron-chunk" in _classes(node) else node.select(".o-pron-chunk")
    for chunk in chunks:
        geo = chunk.select_one(".o-pron-geo")
        phon = chunk.select_one(".o-pron-phon")
        notation = clean_text(phon.get_text(" ", strip=True)) if phon else ""
        if not notation:
            continue
        geo_text = clean_text(geo.get_text(" ", strip=True)) if geo else ""
        region = "UK" if geo_text == "BrE" else "US" if geo_text == "NAmE" else "other"
        audio = chunk.select_one('a[href^="sound://audio/word/"]')
        filename = _audio_filename(str(audio.get("href", ""))) if audio else None
        if filename:
            metrics.audio_references.add(filename)
        item_key = (region, notation, filename)
        if item_key in seen:
            continue
        seen.add(item_key)
        audio_url = f"/audio/oald10/{quote(filename, safe='')}" if filename else None
        pronunciations.append(Pronunciation(region=region, notation=notation, audio_url=audio_url))
    return pronunciations


def _merge_pronunciations(*groups: list[Pronunciation]) -> list[Pronunciation]:
    result: list[Pronunciation] = []
    seen: set[tuple[str, str, str | None]] = set()
    for item in (pronunciation for group in groups for pronunciation in group):
        key = (item.region, item.notation, item.audio_url)
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result


def _collect_audio_references(node: Tag, metrics: ParseMetrics) -> None:
    for link in node.select('a[href^="sound://audio/word/"]'):
        filename = _audio_filename(str(link.get("href", "")))
        if filename:
            metrics.audio_references.add(filename)


def _example_text(node: Tag) -> str:
    """Return rendered example text without audio-link contents or HTML markup."""

    chunks: list[str] = []
    for text_node in node.find_all(string=True):
        audio_link = text_node.find_parent("a")
        if audio_link is not None:
            href = str(audio_link.get("href", ""))
            if href.casefold().startswith("sound://audio/example/"):
                continue
        chunks.append(str(text_node))
    return clean_text("".join(chunks))


def _parse_examples(node: Tag, metrics: ParseMetrics) -> list[Example]:
    examples: list[Example] = []
    for example_list in node.find_all(class_="o-example-list", recursive=False):
        list_id = id(example_list)
        metrics.imported_example_list_ids.add(list_id)
        for item in example_list.find_all("li", recursive=False):
            item_id = id(item)
            if item_id in metrics.imported_example_node_ids:
                metrics.duplicate_example_imports += 1
                continue
            metrics.imported_example_node_ids.add(item_id)
            english_tag = item.select_one(".o-example-eng")
            chinese_tag = item.select_one(".o-example-simp")
            english = _example_text(english_tag) if english_tag else ""
            chinese = _example_text(chinese_tag) if chinese_tag else ""
            if not english:
                metrics.missing_english_examples += 1
            if not chinese:
                metrics.missing_chinese_examples += 1
            if english and chinese:
                examples.append(Example(en=english, zh=chinese))
    return examples


def _example_is_excluded(item: Tag) -> bool:
    if item.find_parent(class_="o-unbox-panel") is not None:
        return True
    panel = item.find_parent(class_="o-pos-panel")
    return panel is not None and bool(_EXCLUDED_COLLECTION_CLASSES.intersection(_classes(panel)))


def _parse_labels(head: Tag | None) -> Labels:
    if head is None:
        return Labels()

    buckets: dict[str, list[str]] = {
        "grammar": [],
        "register": [],
        "region": [],
        "usage": [],
        "construction": [],
        "note": [],
    }
    class_map = {
        "o-gram": "grammar",
        "o-reg": "register",
        "o-geo": "region",
        "o-sense-usage": "usage",
        "o-usage": "usage",
        "o-cf": "construction",
        "o-note": "note",
        "o-help": "note",
    }
    handled: set[int] = set()
    for class_name, bucket in class_map.items():
        for tag in head.select(f".{class_name}"):
            text = clean_text(tag.get_text(" ", strip=True))
            if text:
                buckets[bucket].append(text)
            handled.add(id(tag))

    ignored_classes = {"o-sn-g", "o-label-g", "o-gram-g", "o-symbol-tip"}
    for tag in head.select(".o-tag"):
        if id(tag) in handled or ignored_classes.intersection(_classes(tag)):
            continue
        text = clean_text(tag.get_text(" ", strip=True))
        if text:
            buckets["note"].append(text)

    return Labels(**{name: _unique(values) for name, values in buckets.items()})


def _parse_references(node: Tag, labels: Labels | None = None) -> list[CrossReference]:
    references: list[CrossReference] = []
    seen: set[tuple[str, str, str | None]] = set()
    for link in node.select('a[href^="entry://"]'):
        href = str(link.get("href", ""))
        target = clean_headword(unquote(urlparse(href).netloc + urlparse(href).path))
        display = clean_headword(link.get_text(" ", strip=True)) or target
        reference_container = link.find_parent(class_="o-ref")
        relation_tag = (
            reference_container.select_one(".o-ref-label, .o-ref-text")
            if reference_container
            else None
        )
        relation = clean_text(relation_tag.get_text(" ", strip=True)) if relation_tag else None
        if relation in {"=", "→", ""}:
            relation = None
        key = (target.casefold(), display.casefold(), relation)
        if not target or key in seen:
            continue
        seen.add(key)
        references.append(
            CrossReference(
                target=target,
                display=display,
                relation=relation,
                labels=labels or Labels(),
            )
        )
    return references


def _parse_sense(
    node: Tag,
    metrics: ParseMetrics,
    *,
    english_only_as_reference: bool = False,
) -> list[SenseNode]:
    define = node.select_one(":scope > .o-sense-define")
    if define is None:
        raise ParseError("sense has no direct .o-sense-define")

    head = define.select_one(":scope > .o-sense-head")
    labels = _parse_labels(head)
    index_tag = head.select_one(".o-sn-g") if head else None
    index = clean_text(index_tag.get_text(" ", strip=True)).rstrip(".") if index_tag else None
    english_tag = define.select_one(":scope > .o-def-eng")
    chinese_tag = define.select_one(":scope > .o-def-simp")
    english = clean_text(english_tag.get_text(" ", strip=True)) if english_tag else ""
    chinese = clean_text(chinese_tag.get_text(" ", strip=True)) if chinese_tag else ""
    examples = _parse_examples(node, metrics)
    references = _parse_references(node, labels)

    definition: Definition | None = None
    if english and chinese:
        metrics.definition_pairs += 1
        definition = Definition(en=english, zh=chinese)
    elif english:
        if english_only_as_reference:
            references.insert(
                0,
                CrossReference(
                    target=clean_headword(english),
                    display=english,
                    relation="short form of",
                    labels=labels,
                ),
            )
        else:
            metrics.english_only_definitions += 1
    elif chinese:
        metrics.chinese_only_definitions += 1
    metrics.cross_references += len(references)
    if definition is not None or examples or references:
        return [
            Sense(
                index=index,
                labels=labels,
                definition=definition,
                examples=examples,
                cross_references=references,
            )
        ]

    # Empty source shells carry no output information.
    return []


def _group_title(node: Tag) -> Definition:
    title = node.select_one(":scope > .o-shortcut-title")
    if title is None:
        return Definition(en="", zh="")
    english_tag = title.select_one(":scope > .o-eng")
    chinese_tag = title.select_one(":scope > .o-simp")
    return Definition(
        en=clean_text(english_tag.get_text(" ", strip=True)) if english_tag else "",
        zh=clean_text(chinese_tag.get_text(" ", strip=True)) if chinese_tag else "",
    )


def _parse_root_senses(
    detail: Tag,
    metrics: ParseMetrics,
    *,
    english_only_as_reference: bool,
) -> list[SenseNode]:
    senses: list[SenseNode] = []
    for child in detail.find_all(recursive=False):
        classes = _classes(child)
        if "o-sense" in classes:
            senses.extend(
                _parse_sense(
                    child,
                    metrics,
                    english_only_as_reference=english_only_as_reference,
                )
            )
        elif {"o-multiple-sense", "o-shortcut"}.issubset(classes):
            group_senses: list[SenseNode] = []
            for sense_node in child.find_all(class_="o-sense", recursive=False):
                group_senses.extend(
                    _parse_sense(
                        sense_node,
                        metrics,
                        english_only_as_reference=english_only_as_reference,
                    )
                )
            senses.append(SenseGroup(title=_group_title(child), senses=group_senses))
    return senses


def _parse_child(node: Tag, headword_selector: str, metrics: ParseMetrics) -> ChildEntry:
    headword_tag = node.select_one(headword_selector)
    if headword_tag is None:
        headword_tag = node.select_one(":scope > .o-shortcut-title")
    if headword_tag is None:
        raise ParseError(f"embedded entry lacks {headword_selector} and a title")
    senses: list[SenseNode] = []
    for sense_node in node.find_all(class_="o-sense", recursive=False):
        senses.extend(_parse_sense(sense_node, metrics))
    references = _panel_references(node, metrics)
    return ChildEntry(
        headword=clean_headword(headword_tag.get_text(" ", strip=True)),
        pronunciation=_parse_pronunciations(node, metrics),
        sense=senses,
        cross_references=references,
    )


def _parse_children(
    panel: Tag,
    selector: str,
    headword_selector: str,
    metrics: ParseMetrics,
) -> list[ChildEntry]:
    children: list[ChildEntry] = []
    seen: set[tuple[str, str]] = set()
    for node in panel.select(selector):
        child = _parse_child(node, headword_selector, metrics)
        first_sense = child.sense[0].model_dump_json() if child.sense else ""
        key = (child.headword.casefold(), first_sense)
        if key not in seen:
            seen.add(key)
            children.append(child)
    return children


def _parse_derivatives(panel: Tag, metrics: ParseMetrics) -> list[ChildEntry]:
    derivatives: list[ChildEntry] = []
    for item in panel.select(".o-relations .o-relation-item"):
        head_link = item.select_one('.o-relation-head a[href^="entry://"]')
        head_tag = item.select_one(".o-relation-head .o-dr, .o-relation-head-link")
        if head_tag is None:
            raise ParseError("derivative has no headword")
        pos_tag = item.select_one(".o-relation-pos .o-pos-tag")
        senses: list[SenseNode] = []
        for sense_node in item.find_all(class_="o-sense", recursive=False):
            senses.extend(_parse_sense(sense_node, metrics))
        references: list[CrossReference] = []
        if head_link:
            relation_head = item.select_one(".o-relation-head")
            if relation_head is None:
                raise ParseError("derivative relation link has no relation head")
            references = _parse_references(relation_head)
            metrics.cross_references += len(references)
        derivatives.append(
            ChildEntry(
                headword=clean_headword(head_tag.get_text(" ", strip=True)),
                pos=clean_text(pos_tag.get_text(" ", strip=True)) if pos_tag else None,
                pronunciation=_parse_pronunciations(item, metrics),
                sense=senses,
                cross_references=references,
            )
        )
    return derivatives


def _outside_embedded_entry(tag: Tag, panel: Tag) -> bool:
    parent = tag.parent
    while isinstance(parent, Tag) and parent is not panel:
        classes = _classes(parent)
        if classes.intersection({"o-sense", "o-idiom", "o-phrasal", "o-relation-item"}):
            return False
        parent = parent.parent
    return True


def _panel_references(panel: Tag, metrics: ParseMetrics) -> list[CrossReference]:
    containers = [
        container
        for container in panel.select(".o-reference")
        if _outside_embedded_entry(container, panel)
    ]
    references: list[CrossReference] = []
    seen: set[tuple[str, str, str | None]] = set()
    for container in containers:
        for reference in _parse_references(container):
            key = (reference.target.casefold(), reference.display.casefold(), reference.relation)
            if key not in seen:
                seen.add(key)
                references.append(reference)
    metrics.cross_references += len(references)
    return references


def _panel_number(panel: Tag) -> str | None:
    for class_name in _classes(panel):
        if class_name.startswith("o-for-pos-"):
            return class_name.removeprefix("o-for-pos-")
    return None


def _phrase_kind(panel: Tag) -> tuple[str, str, str] | None:
    phrasal = panel.select_one(".o-phrasal")
    if phrasal is not None:
        return "phrasal verb", ".o-pv", ".o-phrasal"
    idiom = panel.select_one(".o-idiom")
    if idiom is not None:
        return "idiom", ".o-idm", ".o-idiom"
    return None


def _root_panel_pronunciations(detail: Tag, metrics: ParseMetrics) -> list[Pronunciation]:
    pronunciations: list[Pronunciation] = []
    embedded_classes = {"o-idiom", "o-phrasal", "o-relation-item"}
    for chunk in detail.select(".o-pron-chunk"):
        parent = chunk.parent
        embedded = False
        while isinstance(parent, Tag) and parent is not detail:
            if embedded_classes.intersection(_classes(parent)):
                embedded = True
                break
            parent = parent.parent
        if not embedded:
            pronunciations.extend(_parse_pronunciations(chunk, metrics))
    return pronunciations


def _parse_supplemental_record(  # noqa: C901 - isolated legacy supplemental DOM
    soup: BeautifulSoup,
    record_number: int,
    source_key: str,
    metrics: ParseMetrics,
) -> ParsedRecord:
    """Parse the single legacy supplemental-entry DOM found in this source."""
    headword_tag = soup.select_one("main.oald-supplemental-entry .o-headword")
    if headword_tag is None:
        raise ParseError("supplemental record has no headword")
    headword = clean_headword(headword_tag.get_text(" ", strip=True)) or source_key
    main = soup.select_one("main.oald-supplemental-entry")
    if main is not None:
        _collect_audio_references(main, metrics)
    pronunciations: list[Pronunciation] = []
    for selector, region in ((".phons_br .phon", "UK"), (".phons_n_am .phon", "US")):
        for phonetic in soup.select(selector):
            notation = clean_text(phonetic.get_text(" ", strip=True))
            if notation:
                pronunciations.append(Pronunciation(region=region, notation=notation))

    senses: list[SenseNode] = []
    for sense_node in soup.select(".sense_single > li.sense"):
        english_tag = sense_node.select_one(":scope .sensetop > .def")
        chinese_tag = sense_node.select_one(":scope .sensetop > deft chn")
        english = clean_text(english_tag.get_text(" ", strip=True)) if english_tag else ""
        chinese = clean_text(chinese_tag.get_text(" ", strip=True)) if chinese_tag else ""
        references = _parse_references(sense_node)
        metrics.cross_references += len(references)
        if english and chinese:
            metrics.definition_pairs += 1
            senses.append(
                Sense(
                    definition=Definition(en=english, zh=chinese),
                    cross_references=references,
                )
            )
        elif english:
            metrics.english_only_definitions += 1
        elif chinese:
            metrics.chinese_only_definitions += 1
    if not senses:
        raise ParseError("supplemental record has no bilingual sense")
    return ParsedRecord(
        entries=[
            Entry(
                entry_id=(record_number << 10) | 1,
                headword=headword,
                section="proper noun",
                entry_type="word",
                pos="proper noun",
                pronunciation=pronunciations,
                sense=senses,
            )
        ],
        metrics=metrics,
    )


def parse_record(  # noqa: C901 - mirrors the two verified OALD panel variants directly
    html: str | bytes,
    record_number: int,
    source_key: str,
) -> ParsedRecord:
    """Convert one one-based MDX record into independent part-of-speech entries."""

    if record_number <= 0:
        raise ValueError("record_number must be one-based")
    if isinstance(html, bytes):
        html = html.decode("utf-8", errors="strict")
    if html.lstrip().startswith("@@@LINK="):
        raise ParseError("redirect records must be resolved before HTML parsing")

    soup = BeautifulSoup(html, "lxml")
    metrics = ParseMetrics()
    raw_example_items = soup.select(".o-example-list > li")
    metrics.raw_examples = len(raw_example_items)
    excluded_example_node_ids = {
        id(item) for item in raw_example_items if _example_is_excluded(item)
    }
    raw_audio_references: set[str] = set()
    for link in soup.select('a[href^="sound://audio/word/"]'):
        filename = _audio_filename(str(link.get("href", "")))
        if filename:
            raw_audio_references.add(filename)
    for selector in _REMOVED_SELECTORS:
        for node in soup.select(selector):
            node.decompose()

    main = soup.select_one("main.oald-entry")
    if main is not None and "oald-supplemental-entry" in _classes(main):
        return _parse_supplemental_record(soup, record_number, source_key, metrics)
    headword_tag = soup.select_one(".o-head-main .o-h, .o-head-main")
    if main is None or headword_tag is None:
        raise ParseError("record is not an OALD entry")
    headword = clean_headword(headword_tag.get_text(" ", strip=True)) or clean_headword(source_key)
    entry_type = "phrase" if "oald-phrase-entry" in _classes(main) else "word"
    metrics.audio_references.update(raw_audio_references)
    _collect_audio_references(main, metrics)

    pronunciation_sets = {
        str(node.get("data-pronunciation-for")): node
        for node in soup.select(".o-pronunciation-set[data-pronunciation-for]")
    }
    panels: list[tuple[Tag, str, bool]] = []
    for panel in soup.select(".o-pos-panel"):
        panel_classes = _classes(panel)
        pos_tag = panel.select_one(".o-pos-detail > .o-pos-tag")
        detail = panel.select_one(".o-pos-detail")
        is_phrase = "o-phrase-panel" in panel_classes
        if pos_tag is not None:
            panels.append((panel, clean_text(pos_tag.get_text(" ", strip=True)), False))
        elif is_phrase:
            phrase_kind = _phrase_kind(panel)
            if phrase_kind:
                panels.append((panel, phrase_kind[0], True))
        elif detail is not None and detail.select_one(":scope > .o-sense") is not None:
            panels.append((panel, "entry", False))
        elif not panel_classes.intersection(
            {"o-phrase-collection", "o-idiom-collection", "o-phrasal-collection"}
        ):
            phrase_kind = _phrase_kind(panel)
            if phrase_kind:
                panels.append((panel, phrase_kind[0], True))

    if not panels:
        raise ParseError("record contains no actual part-of-speech or phrase panel")
    if len(panels) > MAX_LOCAL_ENTRIES:
        raise ParseError("record exceeds the 1023 local entry ID limit")

    entries: list[Entry] = []
    for local_number, (panel, pos, is_phrase) in enumerate(panels, start=1):
        detail = panel.select_one(".o-pos-detail")
        if detail is None:
            raise ParseError("entry panel has no .o-pos-detail")
        panel_number = _panel_number(panel)
        pronunciation_node = pronunciation_sets.get(panel_number or "")
        pronunciation = (
            _parse_pronunciations(pronunciation_node, metrics)
            if pronunciation_node is not None
            else []
        )
        pronunciation = _merge_pronunciations(
            pronunciation,
            _root_panel_pronunciations(detail, metrics),
        )

        if is_phrase:
            phrase_kind = _phrase_kind(panel)
            if phrase_kind is None:
                raise ParseError("phrase panel has no classified phrase type")
            embedded = panel.select_one(phrase_kind[2])
            if embedded is None:
                raise ParseError("phrase panel contains no embedded phrase")
            child = _parse_child(embedded, phrase_kind[1], metrics)
            entry_headword = child.headword or headword
            senses = child.sense
            cross_references = child.cross_references
            idioms: list[ChildEntry] = []
            phrasal_verbs: list[ChildEntry] = []
            derivatives: list[ChildEntry] = []
        else:
            entry_headword = headword
            senses = _parse_root_senses(
                detail,
                metrics,
                english_only_as_reference="'" in headword or "\u2019" in headword,
            )
            idioms = _parse_children(panel, ".o-idiom", ".o-idm", metrics)
            phrasal_verbs = _parse_children(panel, ".o-phrasal", ".o-pv", metrics)
            derivatives = _parse_derivatives(panel, metrics)
            cross_references = _panel_references(panel, metrics)

        entries.append(
            Entry(
                entry_id=(record_number << 10) | local_number,
                headword=entry_headword,
                section=pos,
                entry_type="phrase" if is_phrase else entry_type,
                pos=pos,
                pronunciation=pronunciation,
                sense=senses,
                child_idioms=idioms,
                child_phrasal_verbs=phrasal_verbs,
                child_derivatives=derivatives,
                cross_references=cross_references,
            )
        )
    imported_example_node_ids = metrics.imported_example_node_ids
    metrics.retained_examples = len(imported_example_node_ids)
    metrics.retained_example_lists = len(metrics.imported_example_list_ids)
    metrics.excluded_examples = len(excluded_example_node_ids)
    raw_example_node_ids = {id(item) for item in raw_example_items}
    metrics.orphan_examples = len(
        raw_example_node_ids - excluded_example_node_ids - imported_example_node_ids
    )
    return ParsedRecord(entries=entries, metrics=metrics)

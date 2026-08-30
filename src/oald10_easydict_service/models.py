"""Structured bilingual dictionary entry models."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    """Base model that rejects undeclared output fields."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class Labels(StrictModel):
    """Sense labels grouped by their editorial purpose."""

    grammar: list[str] = Field(default_factory=list)
    register_labels: list[str] = Field(
        default_factory=list,
        alias="register",
        serialization_alias="register",
    )
    region: list[str] = Field(default_factory=list)
    usage: list[str] = Field(default_factory=list)
    construction: list[str] = Field(default_factory=list)
    note: list[str] = Field(default_factory=list)


class Definition(StrictModel):
    """A paired English and Simplified Chinese definition."""

    en: str
    zh: str


class Example(StrictModel):
    """A paired English and Simplified Chinese example sentence."""

    en: str
    zh: str


class Pronunciation(StrictModel):
    """A regional pronunciation and its local audio endpoint."""

    region: Literal["UK", "US", "other"]
    notation: str
    audio_url: str | None = None


class CrossReference(StrictModel):
    """A definition-like link to another dictionary entry."""

    kind: Literal["cross-reference"] = "cross-reference"
    target: str
    display: str
    relation: str | None = None
    labels: Labels = Field(default_factory=Labels)


class Sense(StrictModel):
    """A sense containing a definition, examples, editorial links, or a combination."""

    kind: Literal["sense"] = "sense"
    index: str | None = None
    labels: Labels = Field(default_factory=Labels)
    definition: Definition | None = None
    examples: list[Example] = Field(default_factory=list)
    cross_references: list[CrossReference] = Field(default_factory=list)


class SenseGroup(StrictModel):
    """An ordered group of related senses."""

    kind: Literal["group"] = "group"
    title: Definition
    senses: list["SenseNode"] = Field(default_factory=list)


SenseNode = Annotated[Sense | SenseGroup, Field(discriminator="kind")]


class ChildEntry(StrictModel):
    """An idiom, phrasal verb, or derivative embedded in an entry."""

    headword: str
    pos: str | None = None
    pronunciation: list[Pronunciation] = Field(default_factory=list)
    sense: list[SenseNode] = Field(default_factory=list)
    cross_references: list[CrossReference] = Field(default_factory=list)


class Entry(StrictModel):
    """The fixed OALD10 entry contract stored in EasyDict SQLite."""

    dict_id: Literal["oald10"] = "oald10"
    entry_id: int = Field(gt=0)
    headword: str
    page: Literal["oald10"] = "oald10"
    section: str
    entry_type: Literal["word", "phrase"]
    pos: str
    pronunciation: list[Pronunciation] = Field(default_factory=list)
    sense: list[SenseNode] = Field(default_factory=list)
    child_idioms: list[ChildEntry] = Field(default_factory=list)
    child_phrasal_verbs: list[ChildEntry] = Field(default_factory=list)
    child_derivatives: list[ChildEntry] = Field(default_factory=list)
    cross_references: list[CrossReference] = Field(default_factory=list)


class ParseMetrics(StrictModel):
    """Per-record counters accumulated by the full build audit."""

    definition_pairs: int = 0
    english_only_definitions: int = 0
    chinese_only_definitions: int = 0
    cross_references: int = 0
    raw_examples: int = 0
    retained_examples: int = 0
    retained_example_lists: int = 0
    excluded_examples: int = 0
    missing_english_examples: int = 0
    missing_chinese_examples: int = 0
    orphan_examples: int = 0
    duplicate_example_imports: int = 0
    imported_example_node_ids: set[int] = Field(default_factory=set, exclude=True)
    imported_example_list_ids: set[int] = Field(default_factory=set, exclude=True)
    audio_references: set[str] = Field(default_factory=set)


class ParsedRecord(StrictModel):
    """All part-of-speech entries produced from one MDX record."""

    entries: list[Entry]
    metrics: ParseMetrics

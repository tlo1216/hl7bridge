"""Field access for the mappers, with a record of every field read.

Every mapper in ``hl7bridge.v2fhir`` reads the message through a
``FieldReader``. The reader records each field position it was asked for
(``PID-5``), per mapper, so a test can check that no mapper reads a field
the mapping spec does not list (``spec.py``). A mapper that reaches past
the reader to the parsed message would escape the check; the drift test
also scans the mapper sources for that.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager

import hl7

from ..ingest.parser import get_field, get_segment_count, split_field_ref


class FieldReader:
    def __init__(self, message: hl7.Message) -> None:
        self._message = message
        self._mapper = "message"
        self.touched: dict[str, set[str]] = defaultdict(set)

    @contextmanager
    def mapper(self, name: str) -> Iterator[None]:
        """Attribute the reads inside the block to mapper ``name``."""
        saved = self._mapper
        self._mapper = name
        try:
            yield
        finally:
            self._mapper = saved

    def _touch(self, ref: str) -> None:
        split_field_ref(ref)  # a bad reference is a bug: raise, do not record it
        self.touched[self._mapper].add(ref)

    def get(self, ref: str, *, rep: int = 1, comp: int = 1, seg: int = 1) -> str:
        """The unescaped value of one component, or '' when absent."""
        self._touch(ref)
        return get_field(self._message, ref, repetition=rep, component=comp, segment_num=seg)

    def repetitions(self, ref: str, *, seg: int = 1) -> int:
        """How many repetitions the field holds (0 when it is empty or absent)."""
        self._touch(ref)
        name, num = split_field_ref(ref)
        try:
            segment = self._message.segments(name)[seg - 1]
        except (KeyError, IndexError):
            return 0
        if num >= len(segment):
            return 0
        text = str(segment[num])
        if not text:
            return 0
        return len(text.split(self._message.separators[2]))

    def count(self, segment: str) -> int:
        """How many segments of this kind the message holds."""
        return get_segment_count(self._message, segment)

    def order(self) -> list[str]:
        """Segment ids in message order."""
        return [str(segment[0]) for segment in self._message]

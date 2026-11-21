"""Field access for the mappers, with a record of every field read.

Every mapper in ``hl7bridge.v2fhir`` reads the message through a
``FieldReader``. The reader records each field position it was asked for
(``PID-5``), per mapper, so a test can check that no mapper reads a field
the mapping spec does not list (``spec.py``). A mapper that reaches past
the reader to the parsed message would escape the check; the drift test
also scans the mapper sources for that.

Every read first checks the field's escape sequences. One that is
unterminated, empty or not decodable is refused with ``MappingError``:
python-hl7 would drop it silently and log the whole field value.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager

import hl7

from ..errors import MappingError
from ..ingest.parser import (
    get_field,
    get_repetition_count,
    get_segment_count,
    has_malformed_escape,
    raw_field,
    split_field_ref,
)


class FieldReader:
    def __init__(self, message: hl7.Message) -> None:
        self._message = message
        self._mapper = "message"
        self._checked: set[tuple[str, int]] = set()
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

    def _touch(self, ref: str, seg: int) -> None:
        split_field_ref(ref)  # a bad reference is a bug: raise, do not record it
        self.touched[self._mapper].add(ref)
        if (ref, seg) in self._checked:
            return
        if has_malformed_escape(raw_field(self._message, ref, segment_num=seg), self._message.esc):
            raise MappingError(ref, "holds a malformed escape sequence", segment=seg)
        self._checked.add((ref, seg))

    def get(
        self, ref: str, *, rep: int = 1, comp: int = 1, seg: int = 1, sub: int | None = None
    ) -> str:
        """The unescaped value of one component, or '' when absent. ``sub``
        picks one subcomponent of it (1-based)."""
        self._touch(ref, seg)
        return get_field(
            self._message,
            ref,
            repetition=rep,
            component=comp,
            segment_num=seg,
            subcomponent=sub,
        )

    def text(self, ref: str, *, rep: int = 1, seg: int = 1) -> str:
        """One whole repetition as text, unescaped, or '' when absent.

        For text types (ST, TX, FT, and NTE-3). A component or subcomponent
        separator that the sender left unescaped stays in the text, so a
        ``^`` in a report line does not cut the line short.
        """
        self._touch(ref, seg)
        raw = raw_field(self._message, ref, segment_num=seg)
        reps = raw.split(self._message.separators[2]) if raw else []
        if rep > len(reps):
            return ""
        return str(self._message.unescape(reps[rep - 1])).strip()

    def repetitions(self, ref: str, *, seg: int = 1) -> int:
        """How many repetitions the field holds (0 when it is empty or absent)."""
        self._touch(ref, seg)
        return get_repetition_count(self._message, ref, segment_num=seg)

    def count(self, segment: str) -> int:
        """How many segments of this kind the message holds."""
        return get_segment_count(self._message, segment)

    def order(self) -> list[str]:
        """Segment ids in message order."""
        return [str(segment[0]) for segment in self._message]

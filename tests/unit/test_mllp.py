"""MLLP codec: framing round-trip, junk visibility, split delivery."""

import asyncio

from hl7bridge.ingest.mllp import Frame, JunkBytes, decode_frames, encode_mllp


class FakeReader:
    """Feeds predetermined chunks, then EOF."""

    def __init__(self, chunks):
        self._chunks = list(chunks)

    async def read(self, _n):
        return self._chunks.pop(0) if self._chunks else b""


def collect(chunks):
    async def run():
        return [item async for item in decode_frames(FakeReader(chunks))]

    return asyncio.run(run())


def test_round_trip_single_frame():
    items = collect([encode_mllp(b"MSH|...")])
    assert items == [Frame(b"MSH|...")]


def test_multiple_frames_one_chunk():
    items = collect([encode_mllp(b"one") + encode_mllp(b"two")])
    assert items == [Frame(b"one"), Frame(b"two")]


def test_frame_split_across_chunks():
    encoded = encode_mllp(b"split-message")
    items = collect([encoded[:4], encoded[4:9], encoded[9:]])
    assert items == [Frame(b"split-message")]


def test_junk_before_frame_is_yielded_not_dropped():
    items = collect([b"noise" + encode_mllp(b"real")])
    assert items == [JunkBytes(b"noise"), Frame(b"real")]


def test_unterminated_frame_at_eof_is_junk():
    items = collect([b"\x0bMSH|never-terminated"])
    assert items == [JunkBytes(b"\x0bMSH|never-terminated")]


def test_bare_junk_at_eof_is_junk():
    items = collect([b"not mllp at all"])
    assert items == [JunkBytes(b"not mllp at all")]


def test_end_block_inside_later_chunk():
    items = collect([b"\x0bpart-one ", b"part-two\x1c\x0d"])
    assert items == [Frame(b"part-one part-two")]

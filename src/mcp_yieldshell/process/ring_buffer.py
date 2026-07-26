"""Byte-capped ring buffers with lossless byte-position cursors."""

from __future__ import annotations

import codecs
from collections.abc import Mapping
from typing import Any


class RingBuffer:
    """Strict byte-capped buffer using global byte positions as cursors.

    Buffers belonging to the same process share ``seq_source``. Each output
    byte therefore has one stable position even when stdout and stderr are
    interleaved. Unlike chunk sequence numbers, byte positions can represent
    a response ending in the middle of a drain chunk without losing output.
    """

    def __init__(self, max_bytes: int, seq_source: list | None = None) -> None:
        self._max_bytes = max_bytes
        self._chunks: list[tuple[int, bytes]] = []  # [(start_position, data), ...]
        self._seq_source: list[int] = seq_source if seq_source is not None else [1]
        self._total_bytes: int = 0
        self._retained_bytes: int = 0
        self._evicted_until_seq: int | None = None

    @property
    def max_bytes(self) -> int:
        return self._max_bytes

    @property
    def byte_count(self) -> int:
        return self._total_bytes

    @property
    def next_seq(self) -> int:
        return self._seq_source[0]

    def append(self, data: bytes) -> None:
        """Append bytes and evict the oldest bytes when over capacity."""
        if not data:
            return
        start_seq = self._seq_source[0]
        if self._chunks:
            previous_start, previous = self._chunks[-1]
            previous_end = previous_start + len(previous)
            previous_is_incomplete = (
                _utf8_safe_prefix_length(previous, len(previous)) < len(previous)
            )
        else:
            previous_start, previous, previous_end = 0, b"", 0
            previous_is_incomplete = False
        if previous_is_incomplete and previous_end == start_seq:
            self._chunks[-1] = (previous_start, previous + data)
        else:
            self._chunks.append((start_seq, data))
        self._total_bytes += len(data)
        self._retained_bytes += len(data)
        self._seq_source[0] += len(data)

        while self._retained_bytes > self._max_bytes and self._chunks:
            start, chunk = self._chunks[0]
            excess = self._retained_bytes - self._max_bytes
            evicted_bytes = min(len(chunk), excess)
            # Do not leave a retained UTF-8 continuation byte at the front.
            while (
                evicted_bytes < len(chunk)
                and chunk[evicted_bytes] & 0b1100_0000 == 0b1000_0000
            ):
                evicted_bytes += 1
            evicted_end = start + evicted_bytes
            self._evicted_until_seq = max(
                self._evicted_until_seq or evicted_end, evicted_end
            )
            if evicted_bytes == len(chunk):
                self._retained_bytes -= len(chunk)
                self._chunks.pop(0)
            else:
                self._chunks[0] = (evicted_end, chunk[evicted_bytes:])
                self._retained_bytes -= evicted_bytes
                break

    def _segments_from(self, since_seq: int | None) -> list[tuple[int, bytes]]:
        cursor = 1 if since_seq is None else since_seq
        segments: list[tuple[int, bytes]] = []
        for start, chunk in self._chunks:
            end = start + len(chunk)
            if end <= cursor:
                continue
            offset = max(0, cursor - start)
            segments.append((start + offset, chunk[offset:]))
        return segments

    def _requested_evicted_data(self, since_seq: int | None) -> bool:
        if self._evicted_until_seq is None:
            return False
        return since_seq is None or since_seq < self._evicted_until_seq

    def read(self, since_seq: int | None = None, max_bytes: int | None = None) -> dict:
        """Read output and return a cursor immediately after returned bytes."""
        effective_max = (
            self._max_bytes if max_bytes is None or max_bytes <= 0 else max_bytes
        )
        result = read_buffers(
            {"text": self},
            since_seq=since_seq,
            max_bytes=effective_max,
        )
        return {
            "text": result["texts"]["text"],
            "start_seq": result["start_seq"],
            "next_seq": result["next_seq"],
            "latest_seq": result["latest_seq"],
            "evicted": result["evicted"],
            "capped": result["capped"],
        }

    def clear(self) -> None:
        """Clear all buffered data."""
        self._chunks.clear()
        self._retained_bytes = 0
        self._evicted_until_seq = None


def read_buffers(
    buffers: Mapping[str, RingBuffer],
    since_seq: int | None,
    max_bytes: int,
) -> dict[str, Any]:
    """Read one or more shared-cursor buffers without skipping capped bytes.

    Output is selected in original cross-stream order and then grouped by
    buffer name for the response. The returned cursor never advances beyond
    a byte omitted because of ``max_bytes``.
    """
    texts = {name: bytearray() for name in buffers}
    segments = sorted(
        (
            (start, name, data)
            for name, buffer in buffers.items()
            for start, data in buffer._segments_from(since_seq)
        ),
        key=lambda item: item[0],
    )
    remaining = max(0, max_bytes)
    next_seq: int | None = None
    start_seq: int | None = None
    capped = False

    for index, (start, name, data) in enumerate(segments):
        if remaining == 0:
            capped = True
            break
        requested = min(len(data), remaining)
        take = _utf8_safe_prefix_length(data, requested)
        if take == 0:
            if next_seq is not None:
                capped = True
                break
            # A single UTF-8 code point may be larger than the requested page.
            # Return that one code point so the cursor still makes progress.
            take = _first_utf8_unit_length(data)
        texts[name].extend(data[:take])
        if start_seq is None:
            start_seq = start
        next_seq = start + take
        remaining = max(0, remaining - take)
        if take < len(data):
            capped = True
            break
        if remaining == 0 and index + 1 < len(segments):
            capped = True
            break

    if not capped:
        cursor = 1 if since_seq is None else since_seq
        covered_ends = [start + len(data) for start, _, data in segments]
        covered_ends.extend(
            buffer._evicted_until_seq or cursor for buffer in buffers.values()
        )
        next_seq = max(covered_ends, default=cursor)
    elif next_seq is None:
        next_seq = 1 if since_seq is None else since_seq

    evicted = any(
        buffer._requested_evicted_data(since_seq) for buffer in buffers.values()
    )
    return {
        "texts": {
            name: bytes(data).decode("utf-8", errors="replace")
            for name, data in texts.items()
        },
        "start_seq": next_seq if start_seq is None else start_seq,
        "next_seq": next_seq,
        "latest_seq": max(
            (buffer.next_seq for buffer in buffers.values()), default=next_seq
        ),
        "evicted": evicted,
        "capped": capped,
    }


def tail_start_seq(
    buffers: Mapping[str, RingBuffer], tail_lines: int, max_bytes: int
) -> int:
    """Return the cursor that starts the last ``tail_lines`` lines of output.

    The result is meant to be passed straight back into :func:`read_buffers`
    as ``since_seq`` so tail reads reuse its cross-stream ordering and UTF-8
    boundary handling. The window is additionally clamped to ``max_bytes`` so
    a tail larger than one response returns its newest bytes rather than its
    oldest.
    """
    segments = sorted(
        (
            (start, data)
            for buffer in buffers.values()
            for start, data in buffer._segments_from(None)
        ),
        key=lambda item: item[0],
    )
    if not segments:
        return max((buffer.next_seq for buffer in buffers.values()), default=1)

    earliest = segments[0][0]
    latest = segments[-1][0] + len(segments[-1][1])
    start = earliest

    if tail_lines > 0:
        remaining_lines = tail_lines
        is_last_segment = True
        for segment_start, data in reversed(segments):
            end = len(data)
            if is_last_segment:
                is_last_segment = False
                # A trailing newline terminates the final line rather than
                # opening an empty one, matching `tail -n`.
                if end > 0 and data[end - 1] == 0x0A:
                    end -= 1
            while end > 0:
                index = data.rfind(b"\n", 0, end)
                if index < 0:
                    break
                remaining_lines -= 1
                if remaining_lines == 0:
                    start = segment_start + index + 1
                    end = -1
                    break
                end = index
            if end < 0:
                break

    if max_bytes > 0 and latest - start > max_bytes:
        start = _advance_past_continuation(segments, latest - max_bytes)
    return start


def _advance_past_continuation(segments: list[tuple[int, bytes]], seq: int) -> int:
    """Move ``seq`` forward off a UTF-8 continuation byte, if it sits on one."""
    for start, data in segments:
        end = start + len(data)
        if not start <= seq < end:
            continue
        offset = seq - start
        while offset < len(data) and data[offset] & 0b1100_0000 == 0b1000_0000:
            offset += 1
        return start + offset
    return seq


def _utf8_safe_prefix_length(data: bytes, limit: int) -> int:
    """Return the largest prefix up to ``limit`` without an incomplete tail."""
    if limit <= 0:
        return 0
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    decoder.decode(data[:limit], final=False)
    pending, _ = decoder.getstate()
    return limit - len(pending)


def _first_utf8_unit_length(data: bytes) -> int:
    """Return enough bytes for one decodable unit, including invalid bytes."""
    for length in range(1, min(4, len(data)) + 1):
        if _utf8_safe_prefix_length(data, length) == length:
            return length
    return min(1, len(data))

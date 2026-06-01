"""Byte-capped ring buffer with chunk sequence numbers for process output."""

from __future__ import annotations


class RingBuffer:
    """Strict byte-capped buffer storing bytes internally with monotonically
    increasing sequence numbers. Supports reads from latest retained output
    or from a given `since_seq`, and reports truncation when data was evicted
    or the read was capped."""

    def __init__(self, max_bytes: int, seq_source: list | None = None) -> None:
        self._max_bytes = max_bytes
        self._chunks: list[tuple[int, bytes]] = []  # [(seq, data), ...]
        self._seq_source: list[int] = seq_source if seq_source is not None else [1]
        self._total_bytes: int = 0
        self._retained_bytes: int = 0
        self._evicted: bool = False

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
        """Append bytes. Evicts oldest data when total exceeds max_bytes."""
        if not data:
            return
        seq = self._seq_source[0]
        self._chunks.append((seq, data))
        self._total_bytes += len(data)
        self._retained_bytes += len(data)
        self._seq_source[0] += 1
        # Evict oldest chunks until within capacity
        while self._retained_bytes > self._max_bytes and self._chunks:
            self._evicted = True
            seq, chunk = self._chunks[0]
            excess = self._retained_bytes - self._max_bytes
            if len(chunk) <= excess:
                # Remove entire first chunk
                self._retained_bytes -= len(chunk)
                self._chunks.pop(0)
            else:
                # Remove part of first chunk
                self._chunks[0] = (seq, chunk[excess:])
                self._retained_bytes -= excess
                break

    def read(self, since_seq: int | None = None, max_bytes: int | None = None) -> dict:
        """Read buffered output.

        Args:
            since_seq: If provided, return output with sequence >= since_seq (from semantics).
                        If None, return all currently retained output.
            max_bytes:  Cap the returned text to this many bytes of output.

        Returns:
            dict with keys: text, next_seq, truncated
        """
        truncated = self._evicted

        # Determine starting chunk index for since_seq filtering
        start_idx = 0
        if since_seq is not None:
            if since_seq >= self._seq_source[0]:
                # Caller has already seen everything
                return {
                    "text": "",
                    "next_seq": self._seq_source[0],
                    "truncated": truncated,
                }
            # Find first chunk with seq >= since_seq
            for i, (seq, _) in enumerate(self._chunks):
                if seq >= since_seq:
                    start_idx = i
                    break
            else:
                # No chunks after since_seq
                return {
                    "text": "",
                    "next_seq": self._seq_source[0],
                    "truncated": truncated,
                }

        # Concatenate data from start_idx onwards
        data = b"".join(chunk_data for _, chunk_data in self._chunks[start_idx:])

        # Apply max_bytes cap
        effective_max = max_bytes or self._max_bytes
        if len(data) > effective_max:
            truncated = True
            data = data[:effective_max]

        # Decode as UTF-8 with replacement
        text = data.decode("utf-8", errors="replace")

        return {
            "text": text,
            "next_seq": self._seq_source[0],
            "truncated": truncated,
        }

    def clear(self) -> None:
        """Clear all buffered data."""
        self._chunks.clear()
        self._retained_bytes = 0
        self._evicted = False

"""Unit tests for the ring buffer."""

from mcp_yieldshell.execution.ring_buffer import (
    RingBuffer,
    read_buffers,
    tail_start_seq,
)


class TestRingBufferAppendRead:
    def test_empty_buffer(self):
        buf = RingBuffer(100)
        result = buf.read()
        assert result["text"] == ""
        assert result["next_seq"] == 1
        assert result["capped"] is False
        assert result["evicted"] is False

    def test_append_and_read(self):
        buf = RingBuffer(100)
        buf.append(b"hello")
        result = buf.read()
        assert result["text"] == "hello"
        assert result["start_seq"] == 1
        assert result["next_seq"] == 6
        assert result["latest_seq"] == 6
        assert result["capped"] is False
        assert result["evicted"] is False

    def test_multiple_appends(self):
        buf = RingBuffer(100)
        buf.append(b"hello ")
        buf.append(b"world")
        result = buf.read()
        assert result["text"] == "hello world"
        assert result["next_seq"] == 12

    def test_byte_count(self):
        buf = RingBuffer(100)
        buf.append(b"hello")
        assert buf.byte_count == 5

    def test_append_empty(self):
        buf = RingBuffer(100)
        buf.append(b"")
        result = buf.read()
        assert result["text"] == ""
        assert result["next_seq"] == 1


class TestRingBufferEviction:
    def test_eviction_on_overflow(self):
        buf = RingBuffer(10)
        buf.append(b"0123456789")  # fills buffer
        buf.append(b"ABCDE")  # overflow, evicts oldest
        result = buf.read()
        assert result["evicted"] is True
        # Should have only the latest data that fits
        assert len(result["text"].encode("utf-8")) <= 10

    def test_eviction_preserves_recent(self):
        buf = RingBuffer(10)
        buf.append(b"0123456789")
        buf.append(b"ABCD")
        result = buf.read()
        assert "ABCD" in result["text"]

    def test_no_eviction_when_within_capacity(self):
        buf = RingBuffer(100)
        buf.append(b"short")
        result = buf.read()
        assert result["evicted"] is False


class TestRingBufferSequence:
    def test_next_seq_increments(self):
        buf = RingBuffer(100)
        assert buf.next_seq == 1
        buf.append(b"chunk1")
        assert buf.next_seq == 7
        buf.append(b"chunk2")
        assert buf.next_seq == 13

    def test_read_with_since_seq_equal_to_next_seq(self):
        buf = RingBuffer(100)
        buf.append(b"hello")
        # since_seq at or past next_seq returns empty
        result = buf.read(since_seq=6)
        assert result["text"] == ""
        assert result["next_seq"] == 6

    def test_read_with_since_seq_past_next_seq(self):
        buf = RingBuffer(100)
        buf.append(b"hello")
        result = buf.read(since_seq=999)
        assert result["text"] == ""

    def test_read_with_since_seq_before_next_seq(self):
        buf = RingBuffer(100)
        buf.append(b"hello")
        buf.append(b" world")
        result = buf.read(since_seq=0)
        assert "hello" in result["text"]

    def test_since_seq_filters_to_new_data(self):
        buf = RingBuffer(100)
        buf.append(b"first")
        buf.append(b" second")
        buf.append(b" third")
        # Position 2 starts inside the first retained chunk.
        result = buf.read(since_seq=2)
        assert "second" in result["text"]
        assert "third" in result["text"]
        assert "first" not in result["text"]

    def test_since_seq_returns_empty_when_no_new_data(self):
        buf = RingBuffer(100)
        buf.append(b"first")
        # next_seq is 6. Querying with since_seq=6 returns empty
        result = buf.read(since_seq=6)
        assert result["text"] == ""
        assert result["next_seq"] == 6

    def test_since_seq_with_eviction(self):
        buf = RingBuffer(10)
        buf.append(b"0123456789")
        buf.append(b"ABCDEF")
        # Position 1 was evicted.
        result = buf.read(since_seq=1)
        assert result["evicted"] is True


class TestRingBufferTruncation:
    def test_read_max_bytes(self):
        buf = RingBuffer(1000)
        buf.append(b"A" * 500)
        result = buf.read(max_bytes=10)
        # Withheld by the response cap, not lost: the rest is still readable.
        assert result["capped"] is True
        assert result["evicted"] is False
        assert result["latest_seq"] == 501
        assert len(result["text"].encode("utf-8")) <= 10

    def test_read_no_truncation_when_within_max(self):
        buf = RingBuffer(1000)
        buf.append(b"short")
        result = buf.read(max_bytes=100)
        assert result["capped"] is False
        assert result["evicted"] is False

    def test_capped_and_evicted_are_independent(self):
        buf = RingBuffer(10)
        buf.append(b"0123456789")
        buf.append(b"ABCDEFGHIJ")

        result = buf.read(since_seq=1, max_bytes=4)

        # Old bytes are gone and the response also stops short of the rest.
        assert result["evicted"] is True
        assert result["capped"] is True
        assert result["text"] == "ABCD"
        assert result["start_seq"] == 11
        assert result["next_seq"] == 15
        assert result["latest_seq"] == 21

    def test_capped_read_cursor_resumes_inside_chunk(self):
        buf = RingBuffer(1000)
        buf.append(b"abcdefghijklmnopqrstuvwxyz")

        first = buf.read(since_seq=1, max_bytes=10)
        second = buf.read(since_seq=first["next_seq"], max_bytes=10)
        third = buf.read(since_seq=second["next_seq"], max_bytes=10)

        assert first["text"] + second["text"] + third["text"] == (
            "abcdefghijklmnopqrstuvwxyz"
        )
        assert first["next_seq"] == 11
        assert second["next_seq"] == 21
        assert third["next_seq"] == 27

    def test_eviction_flag_only_applies_when_requested_data_was_evicted(self):
        buf = RingBuffer(5)
        buf.append(b"old-data")
        fresh_seq = buf.next_seq
        buf.append(b"xy")

        result = buf.read(since_seq=fresh_seq)

        assert result["text"] == "xy"
        assert result["evicted"] is False


class TestRingBufferUTF8:
    def test_invalid_utf8_replacement(self):
        buf = RingBuffer(100)
        buf.append(b"\xff\xfe invalid")
        result = buf.read()
        # Should not crash, should contain replacement chars
        assert isinstance(result["text"], str)
        assert "�" in result["text"] or "invalid" in result["text"]

    def test_valid_utf8(self):
        buf = RingBuffer(100)
        buf.append("こんにちは".encode("utf-8"))
        result = buf.read()
        assert result["text"] == "こんにちは"

    def test_capped_reads_do_not_split_valid_utf8(self):
        buf = RingBuffer(100)
        buf.append("ab€z".encode())

        first = buf.read(since_seq=1, max_bytes=3)
        second = buf.read(since_seq=first["next_seq"], max_bytes=3)
        third = buf.read(since_seq=second["next_seq"], max_bytes=3)

        assert first["text"] + second["text"] + third["text"] == "ab€z"
        assert "�" not in first["text"] + second["text"] + third["text"]

    def test_tiny_page_makes_progress_over_multibyte_character(self):
        buf = RingBuffer(100)
        buf.append("€".encode())

        result = buf.read(since_seq=1, max_bytes=1)

        assert result["text"] == "€"
        assert result["next_seq"] == 4

    def test_eviction_does_not_retain_utf8_continuation_suffix(self):
        buf = RingBuffer(2)
        buf.append("€".encode())

        result = buf.read()

        assert result["text"] == ""
        assert result["next_seq"] == 4
        assert result["evicted"] is True

    def test_utf8_split_across_append_chunks_remains_lossless(self):
        buf = RingBuffer(100)
        encoded = "a€z".encode()
        buf.append(encoded[:2])
        buf.append(encoded[2:])

        cursor = 1
        pages = []
        for _ in range(4):
            result = buf.read(since_seq=cursor, max_bytes=2)
            pages.append(result["text"])
            cursor = result["next_seq"]
            if not result["capped"]:
                break

        assert "".join(pages) == "a€z"
        assert "�" not in "".join(pages)

    def test_clear(self):
        buf = RingBuffer(100)
        buf.append(b"hello")
        total = buf.byte_count
        buf.clear()
        assert buf._retained_bytes == 0
        assert buf.byte_count == total
        result = buf.read()
        assert result["text"] == ""


class TestTailStartSeq:
    def _read_tail(self, buffers, tail_lines, max_bytes=1000):
        start = tail_start_seq(buffers, tail_lines, max_bytes)
        return read_buffers(buffers, since_seq=start, max_bytes=max_bytes)

    def test_returns_last_n_lines(self):
        buf = RingBuffer(1000)
        buf.append(b"one\ntwo\nthree\nfour\n")

        result = self._read_tail({"text": buf}, 2)

        assert result["texts"]["text"] == "three\nfour\n"

    def test_trailing_newline_does_not_count_as_an_empty_line(self):
        buf = RingBuffer(1000)
        buf.append(b"alpha\nbeta\n")

        assert self._read_tail({"text": buf}, 1)["texts"]["text"] == "beta\n"

    def test_unterminated_final_line_is_returned(self):
        buf = RingBuffer(1000)
        buf.append(b"alpha\nbeta")

        assert self._read_tail({"text": buf}, 1)["texts"]["text"] == "beta"

    def test_fewer_lines_available_than_requested(self):
        buf = RingBuffer(1000)
        buf.append(b"only\ntwo\n")

        result = self._read_tail({"text": buf}, 50)

        assert result["texts"]["text"] == "only\ntwo\n"
        assert result["start_seq"] == 1

    def test_empty_buffer(self):
        buf = RingBuffer(1000)

        result = self._read_tail({"text": buf}, 5)

        assert result["texts"]["text"] == ""

    def test_tail_spans_both_streams_in_original_order(self):
        seq_source = [1]
        stdout_buf = RingBuffer(100, seq_source=seq_source)
        stderr_buf = RingBuffer(100, seq_source=seq_source)
        stdout_buf.append(b"out1\n")
        stderr_buf.append(b"err1\n")
        stdout_buf.append(b"out2\n")

        result = self._read_tail(
            {"stdout": stdout_buf, "stderr": stderr_buf}, 2
        )

        assert result["texts"] == {"stdout": "out2\n", "stderr": "err1\n"}

    def test_tail_larger_than_max_bytes_keeps_newest_bytes(self):
        buf = RingBuffer(1000)
        buf.append(b"aaaa\nbbbb\ncccc\n")

        result = self._read_tail({"text": buf}, 3, max_bytes=5)

        # The byte clamp wins, and it keeps the end rather than the start.
        assert result["texts"]["text"] == "cccc\n"

    def test_byte_clamp_does_not_split_a_utf8_character(self):
        buf = RingBuffer(1000)
        buf.append("aa€bb\n".encode())

        result = self._read_tail({"text": buf}, 1, max_bytes=5)

        assert "�" not in result["texts"]["text"]
        assert result["texts"]["text"] == "bb\n"

    def test_tail_after_eviction_starts_at_retained_data(self):
        buf = RingBuffer(10)
        buf.append(b"old-line\n")
        buf.append(b"new\n")

        result = self._read_tail({"text": buf}, 5)

        assert result["texts"]["text"].endswith("new\n")


class TestRingBufferSharedSeq:
    def test_shared_sequence_counter(self):
        seq_source = [1]
        buf_a = RingBuffer(100, seq_source=seq_source)
        buf_b = RingBuffer(100, seq_source=seq_source)
        buf_a.append(b"out1")  # starts at position 1
        buf_b.append(b"err1")  # starts at position 5
        buf_a.append(b"out2")  # starts at position 9
        assert buf_a.next_seq == buf_b.next_seq == 13
        result = buf_a.read(since_seq=9)
        assert "out2" in result["text"]
        assert "out1" not in result["text"]

    def test_shared_seq_read_both_since_seq(self):
        seq_source = [1]
        stdout_buf = RingBuffer(100, seq_source=seq_source)
        stderr_buf = RingBuffer(100, seq_source=seq_source)
        stdout_buf.append(b"out1")  # starts at position 1
        stderr_buf.append(b"err1")  # starts at position 5
        stdout_buf.append(b"out2")  # starts at position 9
        stdout_result = stdout_buf.read(since_seq=5)
        stderr_result = stderr_buf.read(since_seq=5)
        assert "out2" in stdout_result["text"]
        assert "out1" not in stdout_result["text"]
        assert "err1" in stderr_result["text"]

    def test_shared_cursor_cap_does_not_skip_or_duplicate_other_stream(self):
        seq_source = [1]
        stdout_buf = RingBuffer(100, seq_source=seq_source)
        stderr_buf = RingBuffer(100, seq_source=seq_source)
        stdout_buf.append(b"abc")
        stderr_buf.append(b"DEF")
        stdout_buf.append(b"ghi")

        first = read_buffers(
            {"stdout": stdout_buf, "stderr": stderr_buf},
            since_seq=1,
            max_bytes=4,
        )
        second = read_buffers(
            {"stdout": stdout_buf, "stderr": stderr_buf},
            since_seq=first["next_seq"],
            max_bytes=4,
        )
        third = read_buffers(
            {"stdout": stdout_buf, "stderr": stderr_buf},
            since_seq=second["next_seq"],
            max_bytes=4,
        )

        assert first["texts"] == {"stdout": "abc", "stderr": "D"}
        assert second["texts"] == {"stdout": "gh", "stderr": "EF"}
        assert third["texts"] == {"stdout": "i", "stderr": ""}
        assert [first["next_seq"], second["next_seq"], third["next_seq"]] == [
            5,
            9,
            10,
        ]

    def test_uncapped_single_stream_cursor_does_not_skip_trailing_other_stream(self):
        seq_source = [1]
        stdout_buf = RingBuffer(100, seq_source=seq_source)
        stderr_buf = RingBuffer(100, seq_source=seq_source)
        stdout_buf.append(b"OUT1\nOUT2\n")
        stderr_buf.append(b"ERR1\nERR2\n")

        stdout_only = read_buffers(
            {"stdout": stdout_buf},
            since_seq=None,
            max_bytes=100,
        )
        remainder = read_buffers(
            {"stdout": stdout_buf, "stderr": stderr_buf},
            since_seq=stdout_only["next_seq"],
            max_bytes=100,
        )

        assert stdout_only["next_seq"] == 11
        assert remainder["texts"] == {"stdout": "", "stderr": "ERR1\nERR2\n"}
        assert remainder["capped"] is False
        assert remainder["evicted"] is False

    def test_single_stream_tail_byte_cap_ignores_other_stream_cursor_gap(self):
        seq_source = [1]
        stdout_buf = RingBuffer(100, seq_source=seq_source)
        stderr_buf = RingBuffer(100, seq_source=seq_source)
        stdout_buf.append(b"first\n")
        stderr_buf.append(b"x" * 100)
        stdout_buf.append(b"second\n")

        start = tail_start_seq({"stdout": stdout_buf}, tail_lines=2, max_bytes=13)
        result = read_buffers(
            {"stdout": stdout_buf},
            since_seq=start,
            max_bytes=13,
        )

        assert result["texts"]["stdout"] == "first\nsecond\n"
        assert result["capped"] is False

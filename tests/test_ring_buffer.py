"""Unit tests for the ring buffer."""

from mcp_yieldshell.process.ring_buffer import RingBuffer


class TestRingBufferAppendRead:
    def test_empty_buffer(self):
        buf = RingBuffer(100)
        result = buf.read()
        assert result["text"] == ""
        assert result["next_seq"] == 1
        assert result["truncated"] is False

    def test_append_and_read(self):
        buf = RingBuffer(100)
        buf.append(b"hello")
        result = buf.read()
        assert result["text"] == "hello"
        assert result["next_seq"] == 2
        assert result["truncated"] is False

    def test_multiple_appends(self):
        buf = RingBuffer(100)
        buf.append(b"hello ")
        buf.append(b"world")
        result = buf.read()
        assert result["text"] == "hello world"
        assert result["next_seq"] == 3

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
        assert result["truncated"] is True
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
        assert result["truncated"] is False


class TestRingBufferSequence:
    def test_next_seq_increments(self):
        buf = RingBuffer(100)
        assert buf.next_seq == 1
        buf.append(b"chunk1")
        assert buf.next_seq == 2
        buf.append(b"chunk2")
        assert buf.next_seq == 3

    def test_read_with_since_seq_equal_to_next_seq(self):
        buf = RingBuffer(100)
        buf.append(b"hello")
        # since_seq at or past next_seq returns empty
        result = buf.read(since_seq=2)
        assert result["text"] == ""
        assert result["next_seq"] == 2

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
        # since_seq=2 should return data from sequence 2 onwards (chunk 2 and 3)
        result = buf.read(since_seq=2)
        assert "second" in result["text"]
        assert "third" in result["text"]
        assert "first" not in result["text"]

    def test_since_seq_returns_empty_when_no_new_data(self):
        buf = RingBuffer(100)
        buf.append(b"first")
        # next_seq is 2. Querying with since_seq=2 returns empty
        result = buf.read(since_seq=2)
        assert result["text"] == ""
        assert result["next_seq"] == 2

    def test_since_seq_with_eviction(self):
        buf = RingBuffer(10)
        buf.append(b"0123456789")  # fills buffer, seq 1
        buf.append(b"ABCDEF")  # evicts start, seq 2
        # since_seq=1 should return truncated data (chunk 1 was evicted)
        result = buf.read(since_seq=1)
        assert result["truncated"] is True


class TestRingBufferTruncation:
    def test_read_max_bytes(self):
        buf = RingBuffer(1000)
        buf.append(b"A" * 500)
        result = buf.read(max_bytes=10)
        assert result["truncated"] is True
        assert len(result["text"].encode("utf-8")) <= 10

    def test_read_no_truncation_when_within_max(self):
        buf = RingBuffer(1000)
        buf.append(b"short")
        result = buf.read(max_bytes=100)
        assert result["truncated"] is False


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

    def test_clear(self):
        buf = RingBuffer(100)
        buf.append(b"hello")
        total = buf.byte_count
        buf.clear()
        assert buf._retained_bytes == 0
        assert buf.byte_count == total
        result = buf.read()
        assert result["text"] == ""


class TestRingBufferSharedSeq:
    def test_shared_sequence_counter(self):
        seq_source = [1]
        buf_a = RingBuffer(100, seq_source=seq_source)
        buf_b = RingBuffer(100, seq_source=seq_source)
        buf_a.append(b"out1")  # seq 1
        buf_b.append(b"err1")  # seq 2
        buf_a.append(b"out2")  # seq 3
        assert buf_a.next_seq == buf_b.next_seq  # Both see seq=4
        # Reading buf_a with since_seq=3 should return only out2
        result = buf_a.read(since_seq=3)
        assert "out2" in result["text"]
        assert "out1" not in result["text"]

    def test_shared_seq_read_both_since_seq(self):
        seq_source = [1]
        stdout_buf = RingBuffer(100, seq_source=seq_source)
        stderr_buf = RingBuffer(100, seq_source=seq_source)
        stdout_buf.append(b"out1")  # seq 1
        stderr_buf.append(b"err1")  # seq 2
        stdout_buf.append(b"out2")  # seq 3
        # since_seq=2 on both buffers should skip seq 1 data (out1) but include err1 and out2
        stdout_result = stdout_buf.read(since_seq=2)
        stderr_result = stderr_buf.read(since_seq=2)
        assert "out2" in stdout_result["text"]
        assert "out1" not in stdout_result["text"]
        assert "err1" in stderr_result["text"]

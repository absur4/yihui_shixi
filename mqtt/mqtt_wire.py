"""A fixed binary envelope followed by exactly payload_size_bytes application bytes."""
import struct
import zlib

# magic, run UUID, publisher, broadcast target, sequence, perf_counter_ns, length, CRC32, phase
HEADER = struct.Struct('!4s16sHHQQIIB')
MAGIC = b'MQT1'


def pack(run_bytes, publisher, sequence, timestamp, payload, phase=1):
    return HEADER.pack(MAGIC, run_bytes, publisher, 65535, sequence, timestamp,
                       len(payload), zlib.crc32(payload), phase) + payload


def unpack(data, run_bytes, payload_size, publisher_count):
    if len(data) < HEADER.size:
        return None, 'unparseable'
    magic, run, pub, target, seq, sent, size, checksum, phase = HEADER.unpack_from(data)
    if magic != MAGIC:
        return None, 'unparseable'
    if run != run_bytes:
        return None, 'foreign_run'
    if pub >= publisher_count or target != 65535 or phase not in (0, 1, 2) or sent <= 0:
        return None, 'invalid_metadata'
    body = memoryview(data)[HEADER.size:]
    row = (pub, seq, sent, phase)
    if size != payload_size or len(body) != size:
        return row, 'length_error'
    if zlib.crc32(body) != checksum:
        return row, 'checksum_error'
    return row, 'valid'

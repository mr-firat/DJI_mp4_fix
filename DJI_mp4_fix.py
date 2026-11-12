#!/usr/bin/env python3
"""Recover video and DJI metadata tracks from a damaged DJI O3 MP4.

  python DJI_mp4_fix.py DJI_0081.MP4 --reference DJI_0080.MP4 --output recovered.mp4

The reference must be a healthy recording from the same device and settings.
The source is opened read-only and outputs are created exclusively, so no
existing file is ever overwritten. MP4 box layouts follow ISO/IEC 14496-12.
DJI tags and protobuf field numbers were observed in DJI O3 files and are not
MP4-standard identifiers.

Layout of each frame inside the damaged mdat:
  [4-byte AVCC length][H.264 NAL][optional DJI aux block][DJMD][DBGI]
"""

import argparse
import csv
import json
import mmap
import shutil
import statistics
import struct
import subprocess
import sys
import tempfile
from fractions import Fraction
from pathlib import Path
from typing import NamedTuple

# --- H.264 / DJI O3 format values -------------------------------------------
NAL_LENGTH_SIZE = 4  # avcC lengthSizeMinusOne == 3
# NAL header bytes observed in DJI O3 recordings (IDR and non-IDR slices).
NAL_HEADERS = frozenset((0x21, 0x25, 0x41, 0x45, 0x61, 0x65))

DJMD_TAG = 0x1A  # DJI per-frame record tags (single-byte protobuf keys)
DBGI_TAG = 0x12
PB_LEN_1, PB_VARINT_1, PB_VARINT_2, PB_VARINT_3 = 0x0A, 0x08, 0x10, 0x18

DBGI_INIT_SIGNATURE = bytes.fromhex('0a 33 0a 25 0a 05 08')
AUX_BLOCK_SIGNATURE = bytes.fromhex('44 33 22 11')
DJI_US_PER_SECOND = 1_000_000  # DJI timestamps are in microseconds

UINT32_MAX = (1 << 32) - 1
MDAT_32BIT_LIMIT = UINT32_MAX  # largest mdat an 8-byte header can describe
COPY_BLOCK = 8 * 1024 * 1024

# Byte offsets of FullBox fields from the start of the box, by version (v0, v1).
# 'duration' is 32-bit in version 0 and 64-bit in version 1; others are 32-bit.
FULLBOX_FIELDS = {
    'mvhd': {'timescale': (20, 28), 'duration': (24, 32), 'next_track_id': (104, 116)},
    'tkhd': {'track_id': (20, 28), 'duration': (28, 36)},
    'mdhd': {'timescale': (20, 28), 'duration': (24, 32)},
}


# --- Damage report -----------------------------------------------------------

class Report:
    """Collects what was wrong with the source and what the tool did about it."""

    def __init__(self):
        self.damage, self.repairs, self.notes = [], [], []

    def print(self):
        print('\n=== Damage report ===')
        if not self.damage:
            print('No damage found.')
        for title, items in (('Problems found', self.damage),
                             ('Repairs and skipped data', self.repairs), ('Notes', self.notes)):
            if items:
                print(f'{title}:')
                for item in items:
                    print(f'  - {item}')
        print()


def id_ranges(ids):
    """Format [3, 4, 5, 9] as '3–5, 9'."""
    out, ids = [], sorted(ids)
    i = 0
    while i < len(ids):
        j = i
        while j + 1 < len(ids) and ids[j + 1] == ids[j] + 1:
            j += 1
        out.append(str(ids[i]) if i == j else f'{ids[i]}–{ids[j]}')
        i = j + 1
    return ', '.join(out)


# --- MP4 box helpers ---------------------------------------------------------

def box_header(buf, p, end):
    """Return (type, size, header_size, declared_size) of the box at p.

    A declared size of 0 means "to the end of the container". The size is not
    range-checked here.
    """
    if p + 8 > end:
        raise ValueError(f'Truncated box header at {p}')
    size, typ = struct.unpack_from('>I4s', buf, p)
    header = 8
    if size == 1:  # 64-bit largesize follows
        if p + 16 > end:
            raise ValueError(f'Truncated extended box header at {p}')
        size = struct.unpack_from('>Q', buf, p + 8)[0]
        header = 16
    declared = size
    if size == 0:
        size = end - p
    return typ.decode('latin1'), size, header, declared


def iter_boxes(buf, start=0, end=None):
    """Yield (type, offset, size, header_size) for consecutive boxes in buf[start:end]."""
    end = len(buf) if end is None else end
    p = start
    while p < end:
        typ, size, header, _ = box_header(buf, p, end)
        if size < header or p + size > end:
            raise ValueError(f'Invalid {typ!r} box at {p}')
        yield typ, p, size, header
        p += size


def only_box(boxes, name, what):
    found = [b for b in boxes if b[0] == name]
    if len(found) != 1:
        raise ValueError(f'Expected one {name} box in {what}, found {len(found)}')
    return found[0]


def children(box):
    """Return [(type, bytes)] for the child boxes of a container box."""
    header = next(iter_boxes(box))[3]
    return [(t, box[p:p + n]) for t, p, n, _ in iter_boxes(box, header)]


def child(box, *path):
    for name in path:
        box = only_box(children(box), name, 'box')[1]
    return box


def make_box(name, payload):
    if 8 + len(payload) > UINT32_MAX:
        raise ValueError(f'{name} box exceeds 32-bit size')
    return struct.pack('>I4s', 8 + len(payload), name.encode('latin1')) + payload


def map_children(box, fn):
    """Rebuild a container box, replacing each child with fn(type, bytes)."""
    return make_box(box[4:8].decode('latin1'), b''.join(fn(t, b) for t, b in children(box)))


def _fullbox_field(box, name):
    version = box[8]
    if version not in (0, 1):
        raise ValueError(f'Unsupported {box[4:8]!r} version {version}')
    offset = FULLBOX_FIELDS[box[4:8].decode('latin1')][name][version]
    return ('>Q' if name == 'duration' and version == 1 else '>I'), offset


def get_field(box, name):
    fmt, offset = _fullbox_field(box, name)
    return struct.unpack_from(fmt, box, offset)[0]


def set_fields(box, **values):
    out = bytearray(box)
    for name, value in values.items():
        fmt, offset = _fullbox_field(box, name)
        struct.pack_into(fmt, out, offset, value)
    return bytes(out)


def fullbox(name, body):
    return make_box(name, b'\0\0\0\0' + body)  # version 0, flags 0


def stts_box(durations):
    runs = []
    for d in durations:
        if not 1 <= d <= UINT32_MAX:
            raise ValueError('Invalid sample duration')
        if runs and runs[-1][1] == d:
            runs[-1][0] += 1
        else:
            runs.append([1, d])
    return fullbox('stts', struct.pack('>I', len(runs)) + b''.join(struct.pack('>II', *r) for r in runs))


def stsz_box(sizes):
    return fullbox('stsz', struct.pack('>II', 0, len(sizes)) + b''.join(struct.pack('>I', s) for s in sizes))


def chunk_offset_box(offsets):
    """One sample per chunk; use co64 only when a 32-bit offset would overflow."""
    name, fmt = ('stco', '>I') if max(offsets, default=0) <= UINT32_MAX else ('co64', '>Q')
    return fullbox(name, struct.pack('>I', len(offsets)) + b''.join(struct.pack(fmt, o) for o in offsets))


def sample_entry_type(trak):
    stsd = child(trak, 'mdia', 'minf', 'stbl', 'stsd')
    if struct.unpack_from('>I', stsd, 12)[0] < 1:
        raise ValueError('Empty stsd')
    return stsd[20:24].decode('latin1')  # FullBox(12) + entry_count(4) + entry size(4)


def stts_first_last_delta(trak):
    stts = child(trak, 'mdia', 'minf', 'stbl', 'stts')
    count = struct.unpack_from('>I', stts, 12)[0]
    runs = [struct.unpack_from('>II', stts, 16 + 8 * i) for i in range(count)]
    if not runs or any(c < 1 or d < 1 for c, d in runs):
        raise ValueError('Reference stts is empty or invalid')
    return runs[0][1], runs[-1][1]


def rebuild_track(template, track_id, track_duration, media_duration, durations, sizes, offsets):
    """Copy a reference trak, replacing its ID, durations and sample tables."""
    co = chunk_offset_box(offsets)
    tables = {'stts': stts_box(durations),
              'stsc': fullbox('stsc', struct.pack('>IIII', 1, 1, 1, 1)),
              'stsz': stsz_box(sizes), 'stco': co, 'co64': co}

    def stbl(t, b): return tables.get(t, b)
    def minf(t, b): return map_children(b, stbl) if t == 'stbl' else b

    def mdia(t, b):
        if t == 'mdhd':
            return set_fields(b, duration=media_duration)
        return map_children(b, minf) if t == 'minf' else b

    def trak(t, b):
        if t == 'tkhd':
            return set_fields(b, track_id=track_id, duration=track_duration)
        return map_children(b, mdia) if t == 'mdia' else b

    return map_children(template, trak)


# --- DJI metadata parsing ----------------------------------------------------

def read_varint(buf, p, end):
    """Read a protobuf varint (max 64 bits) without reading past end."""
    value = shift = 0
    while p < end and shift <= 63:
        b = buf[p]
        p += 1
        value |= (b & 0x7F) << shift
        if not b & 0x80:
            return value, p
        shift += 7
    raise ValueError('Invalid or truncated varint')


def read_field(buf, p, end, tag, what):
    """Expect a one-byte protobuf key at p and return (varint value, next offset)."""
    if p >= end or buf[p] != tag:
        raise ValueError(what)
    return read_varint(buf, p + 1, end)


def parse_djmd(buf, start, end):
    """DJMD: field 1 { field 1: frame, field 2: timestamp, ... }."""
    n, p = read_field(buf, start, end, PB_LEN_1, 'DJMD outer field')
    inner_end = p + n
    if inner_end > end:
        raise ValueError('DJMD overflow')
    frame, p = read_field(buf, p, inner_end, PB_VARINT_1, 'DJMD frame field')
    timestamp, _ = read_field(buf, p, inner_end, PB_VARINT_2, 'DJMD timestamp field')
    return frame, timestamp


def parse_dbgi(buf, start, end):
    """DBGI: field 1 { field 1 { field 2: timestamp, field 3: frame, ... } }."""
    n, p = read_field(buf, start, end, PB_LEN_1, 'DBGI outer field')
    outer_end = p + n
    if outer_end > end:
        raise ValueError('DBGI overflow')
    n, p = read_field(buf, p, outer_end, PB_LEN_1, 'DBGI inner field')
    inner_end = p + n
    if inner_end > outer_end:
        raise ValueError('DBGI overflow')
    timestamp, p = read_field(buf, p, inner_end, PB_VARINT_2, 'DBGI timestamp field')
    frame, _ = read_field(buf, p, inner_end, PB_VARINT_3, 'DBGI frame field')
    return frame, timestamp


def metadata_pair(mm, dp, end, max_len):
    """Validate a DJMD record at dp followed by a DBGI record.

    Returns (frame, timestamp, dbgi_start, dbgi_end) or None.
    """
    dlen, dstart = read_varint(mm, dp + 1, end)
    dend = dstart + dlen
    if not 1 <= dlen <= max_len or dend >= end or mm[dend] != DBGI_TAG:
        return None
    blen, bstart = read_varint(mm, dend + 1, end)
    bend = bstart + blen
    if not 1 <= blen <= max_len or bend > end:
        return None
    df, dt = parse_djmd(mm, dstart, dend)
    bf, bt = parse_dbgi(mm, bstart, bend)
    if df != bf or dt != bt or df < 1:
        return None
    return df, dt, dend, bend


# --- Source scan -------------------------------------------------------------

class Frame(NamedTuple):
    offset: int       # position of the 4-byte AVCC length prefix
    nal_size: int     # NAL bytes after the prefix (corrected if repaired)
    stored_size: int  # length prefix as found in the file
    nal_header: int
    index: int        # DJI frame number, from 1
    timestamp: int    # DJI microseconds
    djmd_start: int   # DJMD record = [djmd_start, dbgi_start)
    dbgi_start: int   # DBGI record = [dbgi_start, dbgi_end)
    dbgi_end: int

    @property
    def nal_end(self):
        return self.offset + NAL_LENGTH_SIZE + self.nal_size

    @property
    def aux_size(self):
        return self.djmd_start - self.nal_end


class Scan(NamedTuple):
    frames: list
    djmd_init: tuple  # (start, end) of the DJMD initialization block
    dbgi_init: tuple  # (start, end) of the DBGI initialization block


class FrameError(ValueError):
    """A single frame can't be bounded safely; it is dropped, not guessed."""


def find_all(mm, needle, start, end):
    hits, p = [], start
    while (p := mm.find(needle, p, end)) >= 0:
        hits.append(p)
        p += 1
    return hits


def find_source_mdat(mm, report):
    """Walk the source's top-level boxes, tolerating the usual crash damage."""
    end = len(mm)
    p, mdat, others = 0, None, []
    while p < end:
        try:
            typ, size, header, declared = box_header(mm, p, end)
        except ValueError:
            report.damage.append(f'{end - p:,} stray bytes at end of file (offset {p:,}) ignored')
            break
        if typ == 'mdat':
            if mdat:
                raise ValueError('Source has more than one mdat box')
            if declared == 0:
                report.damage.append('mdat size field is 0: the recording was never finalized')
            elif p + size > end:
                report.damage.append(f'mdat declares {size:,} bytes but only {end - p:,} exist: '
                                     'the file is truncated')
                size = end - p
            mdat = (p, size, header)
        elif size < header or p + size > end:
            report.damage.append(f'Truncated or invalid {typ!r} box at offset {p:,} '
                                 f'({end - p:,} bytes) ignored')
            break
        else:
            others.append(typ)
        p += size
    if not mdat:
        raise ValueError('No mdat box found in source')
    if 'moov' in others:
        report.notes.append('Source already has a moov box; it may not need recovery')
    else:
        report.damage.append('moov box is missing: the file has no index and cannot be played')
    return mdat


def protobuf_end(mm, p, end):
    """Skip consecutive well-formed protobuf fields from p; return where they stop.

    Used on the DJI initialization blocks. Frame 1's length prefix starts with
    0x00, which is never a valid protobuf key, so the walk stops exactly there.
    """
    while p < end:
        try:
            key, q = read_varint(mm, p, end)
            field, wire = key >> 3, key & 7
            if field == 0:
                break
            if wire == 0:
                _, q = read_varint(mm, q, end)
            elif wire == 2:
                n, q = read_varint(mm, q, end)
                q += n
            elif wire in (1, 5):
                q += 8 if wire == 1 else 4
            else:
                break
        except ValueError:
            break
        if q > end:
            break
        p = q
    return p


def packets_ending_at(mm, lo, boundary):
    """Offsets p in [lo, boundary) whose AVCC packet ends exactly at boundary."""
    return [p for p in range(lo, boundary - NAL_LENGTH_SIZE)
            if mm[p + NAL_LENGTH_SIZE] in NAL_HEADERS
            and p + NAL_LENGTH_SIZE + struct.unpack_from('>I', mm, p)[0] == boundary]


def read_frame(mm, cursor, index, record, report):
    """Read the packet that starts at cursor and must end before its DJMD record."""
    djmd_start, dbgi_start, dbgi_end, timestamp = record
    if cursor + NAL_LENGTH_SIZE + 1 > djmd_start:
        raise FrameError(f'packet position {cursor:,} overlaps its DJMD record')
    stored = struct.unpack_from('>I', mm, cursor)[0]
    header = mm[cursor + NAL_LENGTH_SIZE]
    if header not in NAL_HEADERS:
        raise FrameError(f'unexpected NAL header 0x{header:02x} at offset {cursor:,}')

    nal_size = stored
    if stored < 1 or cursor + NAL_LENGTH_SIZE + stored > djmd_start:
        # The packet start and its DJMD are both validated, so the true end is
        # the DJMD record, or the DJI aux block in front of it if there is one.
        aux = find_all(mm, AUX_BLOCK_SIGNATURE, cursor + NAL_LENGTH_SIZE + 1, djmd_start)
        if len(aux) > 1:
            raise FrameError(f'NAL length {stored:,} is invalid and the packet end is ambiguous')
        nal_size = (aux[0] if aux else djmd_start) - cursor - NAL_LENGTH_SIZE
        report.repairs.append(f'Frame {index}: NAL length {stored:,} at offset {cursor:,} '
                              f'was invalid; corrected to {nal_size:,} from the metadata boundary')

    frame = Frame(cursor, nal_size, stored, header, index, timestamp, djmd_start, dbgi_start, dbgi_end)
    if frame.aux_size and mm[frame.nal_end:frame.nal_end + len(AUX_BLOCK_SIGNATURE)] != AUX_BLOCK_SIGNATURE:
        # Possibly a damaged, too-short length. Unknown bytes are never treated as video.
        raise FrameError(f'{frame.aux_size:,} unexplained bytes between the NAL (length {stored:,}) '
                         'and its DJMD record')
    return frame


def scan_source(mm, report):
    """Index validated DJMD/DBGI pairs, then walk H.264 packets in frame order."""
    mdat_pos, mdat_size, mdat_header = find_source_mdat(mm, report)
    start, end = mdat_pos + mdat_header, mdat_pos + mdat_size
    max_len = end - start

    # 1. Every structurally valid DJMD+DBGI pair, keyed by frame number.
    metadata = {}
    pos = start
    while (dp := mm.find(bytes([DJMD_TAG]), pos, end)) >= 0:
        pos = dp + 1
        try:
            pair = metadata_pair(mm, dp, end, max_len)
        except (ValueError, IndexError):
            continue
        if pair is None:
            continue
        frame, timestamp, dbgi_start, dbgi_end = pair
        record = (dp, dbgi_start, dbgi_end, timestamp)
        if metadata.setdefault(frame, record) != record:
            raise ValueError(f'Conflicting metadata records for frame {frame}')

    if not metadata:
        raise ValueError('No valid DJMD/DBGI pairs found: this does not look like a DJI O3 recording')
    frame_ids = sorted(metadata)
    if any(metadata[b][0] <= metadata[a][0] or metadata[b][3] <= metadata[a][3]
           for a, b in zip(frame_ids, frame_ids[1:])):
        raise ValueError('Metadata frame numbers are out of file or timestamp order; '
                         'refusing ambiguous reconstruction')
    missing = sorted(set(range(1, frame_ids[-1] + 1)) - set(frame_ids))
    if missing:
        report.damage.append(f'DJI metadata missing or corrupt for {len(missing)} frame(s): '
                             f'{id_ranges(missing)}')

    # 2. The mdat starts with [DJMD init][DBGI init], both sequences of protobuf
    #    fields, followed by frame 1. Walking the fields finds frame 1's start
    #    without trusting its length prefix; frame 1's stored length is the
    #    fallback and the cross-check.
    first_meta = metadata[frame_ids[0]][0]
    sig = find_all(mm, DBGI_INIT_SIGNATURE, start, first_meta)
    if len(sig) != 1 or sig[0] == start:
        raise ValueError(f'Expected one DJMD + DBGI initialization block before frame 1, found {len(sig)}')
    init_end = protobuf_end(mm, sig[0], first_meta)
    # Accept the walk if it lands on a valid NAL header, or on a length prefix
    # that ends exactly at frame 1's DJMD (frame 1's header byte may be damaged).
    walk_ok = init_end + NAL_LENGTH_SIZE < first_meta and (
        mm[init_end + NAL_LENGTH_SIZE] in NAL_HEADERS
        or (1 in metadata and init_end + NAL_LENGTH_SIZE
            + struct.unpack_from('>I', mm, init_end)[0] == metadata[1][0]))
    candidates = packets_ending_at(mm, sig[0], metadata[1][0]) if 1 in metadata else []
    if walk_ok:
        if len(candidates) == 1 and candidates[0] != init_end:
            raise ValueError(f'Frame 1 start is ambiguous: the init block ends at {init_end:,} but '
                             f'frame 1\'s length prefix points to {candidates[0]:,}')
    elif len(candidates) == 1:
        init_end = candidates[0]
        report.damage.append('DBGI initialization block is not well-formed; its end was taken '
                             'from frame 1\'s length prefix')
    else:
        raise ValueError('Neither the DJI initialization block nor frame 1\'s length prefix '
                         'establishes where frame 1 starts. Refusing to guess.')

    # 3. Walk frames in order. A frame starts where the previous frame's DBGI
    #    ends. If that frame's metadata is lost, resynchronize on the single
    #    AVCC packet that ends exactly at this frame's DJMD.
    frames, dropped = [], []
    prev_end = init_end
    for index in frame_ids:
        record = metadata[index]
        if index == 1 or index - 1 in metadata:
            cursor = prev_end
        else:
            candidates = packets_ending_at(mm, prev_end, record[0])
            if len(candidates) != 1:
                cursor = None
                dropped.append(index)
                report.repairs.append(f'Frame {index}: packet start could not be established '
                                      f'({len(candidates)} candidates); frame dropped')
            else:
                cursor = candidates[0]
                report.repairs.append(f'Frame {index}: resynchronized at offset {cursor:,}; '
                                      f'{cursor - prev_end:,} damaged bytes before it skipped')
        prev_end = record[2]
        if cursor is None:
            continue
        try:
            frames.append(read_frame(mm, cursor, index, record, report))
        except FrameError as e:
            dropped.append(index)
            report.damage.append(f'Frame {index}: {e}')
            report.repairs.append(f'Frame {index}: dropped')

    if not frames:
        raise ValueError('No frames could be recovered')

    # 4. Anything after the last complete frame is a truncated frame or junk.
    last_end = metadata[frame_ids[-1]][2]
    if last_end < end:
        tail = end - last_end
        detail = ''
        if tail > NAL_LENGTH_SIZE and mm[last_end + NAL_LENGTH_SIZE] in NAL_HEADERS:
            stored = struct.unpack_from('>I', mm, last_end)[0]
            detail = f' (frame {frame_ids[-1] + 1} cut off: NAL length {stored:,}, ' \
                     f'{tail - NAL_LENGTH_SIZE:,} bytes present)'
        report.damage.append(f'{tail:,} bytes after the last complete frame at offset '
                             f'{last_end:,} not recovered{detail}')

    aux = sum(1 for f in frames if f.aux_size)
    if aux:
        report.notes.append(f'{aux:,} frame(s) carry a DJI auxiliary block (normal); it is not copied')
    report.notes.append(f'Recovered {len(frames):,} of {frame_ids[-1]:,} frames'
                        + (f'; lost: {id_ranges(missing + dropped)}' if missing or dropped else ''))
    return Scan(frames, (start, sig[0]), (sig[0], init_end))


def write_index_csv(frames, path):
    with open(path, 'x', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(['frame_index', 'timestamp_us', 'nal_offset', 'nal_size', 'stored_nal_size',
                    'nal_header', 'aux_offset', 'aux_size', 'djmd_offset', 'dbgi_offset', 'dbgi_end'])
        for fr in frames:
            w.writerow([fr.index, fr.timestamp, fr.offset, fr.nal_size, fr.stored_size,
                        f'0x{fr.nal_header:02x}', fr.nal_end if fr.aux_size else '', fr.aux_size,
                        fr.djmd_start, fr.dbgi_start, fr.dbgi_end])


# --- Output ------------------------------------------------------------------

def copy_range(dest, buf, start, end):
    for p in range(start, end, COPY_BLOCK):
        dest.write(buf[p:min(p + COPY_BLOCK, end)])


def mux_video(mm, frames, reference, output):
    """Mux H.264 packets with DJI timestamps, using the reference video stream as template."""
    try:
        import av
    except ImportError:
        raise RuntimeError('PyAV is required: python -m pip install av')

    time_base = Fraction(1, DJI_US_PER_SECOND)
    t0 = frames[0].timestamp
    if len(frames) > 1:
        last_duration = int(statistics.median(b.timestamp - a.timestamp for a, b in zip(frames, frames[1:])))
    else:
        last_duration = DJI_US_PER_SECOND // 50  # no measurable interval: assume 50 fps
    with av.open(str(reference)) as ref, av.open(
            str(output), mode='w', format='mp4',
            options={'video_track_timescale': str(DJI_US_PER_SECOND)}) as out:
        template = next((s for s in ref.streams if s.type == 'video'), None)
        if template is None:
            raise ValueError('Reference contains no video stream')
        stream = out.add_stream_from_template(template)
        stream.time_base = time_base
        for i, f in enumerate(frames):
            next_ts = frames[i + 1].timestamp if i + 1 < len(frames) else f.timestamp + last_duration
            # Rebuild the length prefix so repaired lengths are written correctly.
            data = f.nal_size.to_bytes(NAL_LENGTH_SIZE, 'big') + mm[f.offset + NAL_LENGTH_SIZE:f.nal_end]
            packet = av.Packet(data)
            packet.pts = packet.dts = f.timestamp - t0
            packet.duration = next_ts - f.timestamp
            packet.time_base = time_base
            packet.stream = stream
            out.mux(packet)
            if (i + 1) % 1000 == 0:
                print(f'  Reconstructed {i + 1:,}/{len(frames):,} video packets')


def reference_metadata_tracks(reference):
    with open(reference, 'rb') as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as rm:
        _, p, n, _ = only_box(list(iter_boxes(rm)), 'moov', 'reference')
        moov = rm[p:p + n]
    tracks = {}
    for t, trak in children(moov):
        if t == 'trak' and (entry := sample_entry_type(trak)) in ('djmd', 'dbgi'):
            if entry in tracks:
                raise ValueError(f'Multiple reference {entry} tracks')
            tracks[entry] = trak
    if set(tracks) != {'djmd', 'dbgi'}:
        raise ValueError('Reference must contain djmd and dbgi tracks')
    return tracks


def add_metadata_tracks(mm, scan, reference, video, output):
    """Append DJMD/DBGI samples to the video's mdat and add matching traks to its moov."""
    frames = scan.frames
    templates = reference_metadata_tracks(reference)
    samples = {
        'djmd': [scan.djmd_init] + [(f.djmd_start, f.dbgi_start) for f in frames],
        'dbgi': [scan.dbgi_init] + [(f.dbgi_start, f.dbgi_end) for f in frames],
    }

    with open(video, 'rb') as vf, mmap.mmap(vf.fileno(), 0, access=mmap.ACCESS_READ) as vm:
        tops = list(iter_boxes(vm))
        _, mdat_pos, mdat_size, mdat_header = only_box(tops, 'mdat', 'recovered video')
        _, moov_pos, moov_size, _ = only_box(tops, 'moov', 'recovered video')
        mdat_end = mdat_pos + mdat_size
        if moov_pos < mdat_end:
            raise ValueError('Video moov must follow the mdat box')
        moov = vm[moov_pos:moov_pos + moov_size]
        mvhd = child(moov, 'mvhd')
        movie_scale = get_field(mvhd, 'timescale')
        video_traks = [b for t, b in children(moov) if t == 'trak']
        if not video_traks:
            raise ValueError('Recovered video moov has no video trak')
        first_id = max(get_field(child(t, 'tkhd'), 'track_id') for t in video_traks) + 1

        # Build the new traks. Sample timing follows the DJI timestamps; the
        # first (init block) and last durations come from the reference stts.
        cursor = mdat_end
        new_traks, track_durations = [], []
        timestamps = [f.timestamp for f in frames]
        for i, kind in enumerate(('djmd', 'dbgi')):
            template = templates[kind]
            scale = get_field(child(template, 'mdia', 'mdhd'), 'timescale')
            first_delta, last_delta = stts_first_last_delta(template)
            pts = [0] + [first_delta + ((t - timestamps[0]) * scale + DJI_US_PER_SECOND // 2) // DJI_US_PER_SECOND
                         for t in timestamps]
            durations = [b - a for a, b in zip(pts, pts[1:])] + [last_delta]
            if any(d <= 0 for d in durations):
                raise ValueError(f'{kind} timestamp mapping creates non-positive duration')
            sizes = [e - s for s, e in samples[kind]]
            offsets = []
            for size in sizes:
                offsets.append(cursor)
                cursor += size
            media_duration = sum(durations)
            track_duration = (media_duration * movie_scale + scale // 2) // scale
            track_durations.append(track_duration)
            new_traks.append(rebuild_track(template, first_id + i, track_duration, media_duration,
                                           durations, sizes, offsets))

        # The enlarged mdat keeps its payload exactly where it is, so no video
        # chunk offset changes. If it outgrows a 32-bit size, the 8-byte 'free'
        # box FFmpeg reserves in front of mdat becomes room for a 16-byte header.
        metadata_bytes = cursor - mdat_end
        payload_size = mdat_size - mdat_header + metadata_bytes
        header_pos = mdat_pos
        if mdat_header == 8 and 8 + payload_size <= MDAT_32BIT_LIMIT:
            mdat_box_header = struct.pack('>I4s', 8 + payload_size, b'mdat')
        else:
            if mdat_header == 8:
                if not any(t in ('free', 'wide') and n == 8 and p + 8 == mdat_pos for t, p, n, _ in tops):
                    raise ValueError('Enlarged mdat exceeds 4 GiB and there is no reserved space '
                                     'in front of it for a 64-bit header')
                header_pos = mdat_pos - 8
                print('Enlarged mdat exceeds 4 GiB: using a 64-bit mdat header')
            mdat_box_header = struct.pack('>I4sQ', 1, b'mdat', 16 + payload_size)

        new_mvhd = set_fields(mvhd, duration=max([get_field(mvhd, 'duration')] + track_durations),
                              next_track_id=first_id + 2)
        parts, inserted = [], False
        for t, b in children(moov):
            parts.append(new_mvhd if t == 'mvhd' else b)
            if t == 'trak' and not inserted:
                parts.extend(new_traks)
                inserted = True
        new_moov = make_box('moov', b''.join(parts))
        expected_size = len(vm) + metadata_bytes + len(new_moov) - moov_size

        print(f'DJMD/DBGI samples: {len(frames) + 1:,} each | '
              f'appended {metadata_bytes:,} bytes | new moov {len(new_moov):,} bytes')

        try:
            with open(output, 'xb') as dest:
                copy_range(dest, vm, 0, header_pos)
                dest.write(mdat_box_header)
                copy_range(dest, vm, mdat_pos + mdat_header, mdat_end)
                for kind in ('djmd', 'dbgi'):
                    for s, e in samples[kind]:
                        dest.write(mm[s:e])
                # Keep every top-level box after mdat (e.g. 'free'), swapping in the new moov.
                for t, p, n, _ in tops:
                    if p >= mdat_end:
                        dest.write(new_moov) if p == moov_pos else copy_range(dest, vm, p, p + n)
            if output.stat().st_size != expected_size:
                raise ValueError('Written output size differs from the predicted size')
        except BaseException:
            output.unlink(missing_ok=True)
            raise


def verify(path, frames, report):
    """Check the tracks with ffprobe, then decode every frame and report picture damage."""
    ffprobe = shutil.which('ffprobe')
    if not ffprobe:
        raise RuntimeError('Install FFmpeg (ffprobe) for output verification')
    probe = subprocess.run(
        [ffprobe, '-v', 'error', '-show_entries', 'stream=codec_type,codec_tag_string,nb_frames',
         '-of', 'json', str(path)], check=True, text=True, capture_output=True)
    streams = json.loads(probe.stdout).get('streams', [])
    tags = {s.get('codec_tag_string') for s in streams}
    if not {'djmd', 'dbgi'} <= tags:
        raise RuntimeError(f'ffprobe did not find both DJI tracks; tags={tags}')
    videos = [s for s in streams if s.get('codec_type') == 'video']
    if not videos:
        raise RuntimeError('ffprobe found no video stream')
    nb = videos[0].get('nb_frames')
    if nb not in (None, 'N/A') and int(nb) != len(frames):
        raise RuntimeError(f'Video frames: {nb}, expected {len(frames)}')

    # H.264 has no checksums, so damage inside picture data is only visible to
    # the decoder: it conceals the error and marks the frame as corrupt.
    import av
    t0 = frames[0].timestamp
    by_time = {f.timestamp - t0: f.index for f in frames}
    decoded, damaged, failed = 0, [], []
    print('Decoding every frame to check for picture damage...')
    with av.open(str(path)) as container:
        stream = container.streams.video[0]
        stream.thread_type = 'AUTO'
        to_index = lambda pts: by_time.get(round((pts or 0) * stream.time_base * DJI_US_PER_SECOND), -1)
        for packet in container.demux(stream):
            try:
                for picture in stream.decode(packet):
                    decoded += 1
                    if picture.is_corrupt:
                        damaged.append(to_index(picture.pts))
            except av.error.FFmpegError:
                failed.append(to_index(packet.pts))
    if damaged:
        report.damage.append(f'Decoder found picture damage in {len(damaged)} frame(s): {id_ranges(damaged)}. '
                             'Expect visual glitches there, possibly until the next keyframe')
    if failed:
        report.damage.append(f'{len(failed)} frame(s) could not be decoded at all: {id_ranges(failed)}')
    if decoded != len(frames):
        report.damage.append(f'Decoder produced {decoded:,} of {len(frames):,} frames')
    if not decoded:
        raise RuntimeError('No video frame could be decoded')
    print('VERIFY: ffprobe found video + djmd + dbgi; decoded '
          f'{decoded:,} frames, {len(damaged) + len(failed):,} with picture damage.')


# --- CLI ---------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description='Recover video and DJI metadata tracks from a damaged DJI O3 MP4')
    ap.add_argument('source', type=Path, help='Damaged MP4; never modified')
    ap.add_argument('--reference', type=Path, required=True,
                    help='Healthy MP4 from the same device and settings')
    ap.add_argument('--output', type=Path, default=Path('recovered_dji.mp4'),
                    help='Output MP4 path (default: recovered_dji.mp4); must not exist')
    ap.add_argument('--index-csv', type=Path, help='Also write the per-frame packet index as CSV')
    args = ap.parse_args()

    inputs = [args.source, args.reference]
    for p in inputs:
        if not p.is_file():
            ap.error(f'Input not found: {p}')
    for out in filter(None, (args.output, args.index_csv)):
        if out.exists():
            ap.error(f'Refusing to overwrite existing file: {out}')
        if out.resolve() in {p.resolve() for p in inputs}:
            ap.error('Output must not be an input file')

    with open(args.source, 'rb') as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
        report = Report()
        try:
            scan = scan_source(mm, report)
            if args.index_csv:
                write_index_csv(scan.frames, args.index_csv)
                print(f'Wrote frame index: {args.index_csv}')
            with tempfile.TemporaryDirectory(prefix='dji_recovery_') as tmp:
                video = Path(tmp) / 'video_only.mp4'
                print('Reconstructing video packets...')
                mux_video(mm, scan.frames, args.reference, video)
                add_metadata_tracks(mm, scan, args.reference, video, args.output)
            print(f'Created {args.output} ({args.output.stat().st_size:,} bytes)')
            verify(args.output, scan.frames, report)
        finally:
            report.print()


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, struct.error, subprocess.CalledProcessError, RuntimeError) as e:
        print(f'ERROR: {e}', file=sys.stderr)
        sys.exit(1)

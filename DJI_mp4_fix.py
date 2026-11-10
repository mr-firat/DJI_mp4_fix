#!/usr/bin/env python3
"""Recover video and DJI metadata tracks from a damaged DJI O3 MP4.

Full recovery (requires a healthy reference from the same device/configuration):
  python recover_dji_mp4.py DJI_0081.MP4 --reference DJI_0080.MP4 --output recovered.mp4

Video-only recovery (no reference; timing is estimated from DJI timestamps):
  python recover_dji_mp4.py DJI_0081.MP4 --output recovered_video_only.mp4 --video-only

The source is read-only. Outputs are created exclusively and never overwrite inputs.
MP4 box layouts follow ISO/IEC 14496-12. DJI metadata tags and protobuf fields are
format-specific values observed in DJI O3 files, not MP4-standard identifiers.
"""

import argparse
import csv
import json
import mmap
import os
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

# ---------------------------------------------------------------------------
# ISO Base Media File Format (MP4 / ISO/IEC 14496-12) constants
# ---------------------------------------------------------------------------
MP4_BOX_SIZE_FIELD_SIZE = 4  # uint32 size field before the four-character type
MP4_BOX_HEADER_SIZE = 8       # uint32 size + four-character box type
MP4_EXTENDED_HEADER_SIZE = 16  # size==1: uint32 size + type + uint64 largesize
MP4_SIZE_EXTENDS_TO_EOF = 0   # size==0 means the box extends to its container end
MP4_SIZE_USES_LARGESIZE = 1   # size==1 means a 64-bit largesize follows
UINT32_MAX = (1 << 32) - 1
UINT64_MAX = (1 << 64) - 1
MP4_FULLBOX_PREFIX_SIZE = MP4_BOX_HEADER_SIZE + 4  # box header + version/flags
MP4_STSD_ENTRY_COUNT_OFFSET = MP4_FULLBOX_PREFIX_SIZE  # stsd: box header + version/flags
MP4_STSD_FIRST_ENTRY_OFFSET = MP4_STSD_ENTRY_COUNT_OFFSET + 4 + MP4_BOX_SIZE_FIELD_SIZE
# stsd entry layout: FullBox prefix, entry_count:uint32, sample_entry_size:uint32,
# then sample_entry_type: four bytes (for example 'avc1', 'djmd' or 'dbgi').
MP4_STTS_ENTRIES_OFFSET = MP4_STSD_ENTRY_COUNT_OFFSET + 4  # FullBox + entry_count
MP4_STTS_ENTRY_SIZE = 8  # Each stts run is sample_count:uint32 + sample_delta:uint32
MP4_FULLBOX_VERSION_OFFSET = MP4_BOX_HEADER_SIZE  # version byte follows box header
MP4_FULLBOX_FLAGS_SIZE = 3

# Version-dependent offsets inside mdhd, tkhd and mvhd boxes. These are
# byte offsets from the beginning of each complete box, including its header.
MDHD_V0_TIMESCALE_OFFSET, MDHD_V0_DURATION_OFFSET = 20, 24
MDHD_V1_TIMESCALE_OFFSET, MDHD_V1_DURATION_OFFSET = 28, 32
TKHD_V0_TRACK_ID_OFFSET, TKHD_V0_DURATION_OFFSET = 20, 28
TKHD_V1_TRACK_ID_OFFSET, TKHD_V1_DURATION_OFFSET = 28, 36
MVHD_V0_TIMESCALE_OFFSET, MVHD_V0_DURATION_OFFSET, MVHD_V0_NEXT_TRACK_ID_OFFSET = 20, 24, 104
MVHD_V1_TIMESCALE_OFFSET, MVHD_V1_DURATION_OFFSET, MVHD_V1_NEXT_TRACK_ID_OFFSET = 28, 32, 116
# ISO BMFF version 0 uses 32-bit creation/modification/duration fields;
# version 1 widens the applicable time fields to 64 bits.
FULLBOX_V0_FIELD_WIDTH = 4
FULLBOX_V1_FIELD_WIDTH = 8

# ---------------------------------------------------------------------------
# H.264 / DJI O3 stream constants
# ---------------------------------------------------------------------------
AVCC_NAL_LENGTH_SIZE = 4  # This DJI sample entry uses lengthSizeMinusOne=3 in avcC,
# so each AVC sample's NAL length prefix occupies 4 bytes (big-endian).
H264_NAL_HEADER_OFFSET = AVCC_NAL_LENGTH_SIZE
H264_MIN_NAL_UNIT_SIZE = 1  # one-byte NAL header is the minimum valid unit
# Observed valid NAL header bytes in these DJI recordings (a conservative whitelist),
# not an exhaustive list of all H.264 NAL-unit types.
H264_NAL_HEADER_TYPES = (0x21, 0x25, 0x41, 0x45, 0x61, 0x65)

# DJI-specific protobuf tags / wire-field identifiers (not MP4 box types).
DJMD_SAMPLE_TAG = 0x1A
DBGI_SAMPLE_TAG = 0x12
PROTOBUF_LENGTH_DELIMITED_FIELD_1 = 0x0A
PROTOBUF_VARINT_FIELD_1 = 0x08
PROTOBUF_VARINT_FIELD_2 = 0x10
PROTOBUF_VARINT_FIELD_3 = 0x18
DJI_PROTOBUF_TAG_SIZE = 1  # these DJI field keys fit in one protobuf byte
DJI_FIRST_FRAME_INDEX = 1  # Observed DJI frame numbering starts at one.
DBGI_INIT_SIGNATURE = bytes.fromhex('0a 33 0a 25 0a 05 08')

# Operational constants.
# The copy buffer is an implementation detail, not an MP4 or DJI format value.
DEFAULT_COPY_BLOCK_SIZE = 8 * 1024 * 1024

# DJI timestamps observed in this O3 metadata format are expressed in
# microseconds. This is a DJI-format assumption, NOT an MP4 timescale.
DJI_TIMESTAMP_TICKS_PER_SECOND = 1_000_000

# A DJI auxiliary-data block signature observed between a video NAL and DJMD.
# A gap is accepted only when it starts with this marker; its size is then
# determined from the next validated metadata boundary, not a guessed limit.
DJI_AUXILIARY_BLOCK_SIGNATURE = bytes.fromhex("44 33 22 11")

COPY_BLOCK = DEFAULT_COPY_BLOCK_SIZE
DJMD_TAG = DJMD_SAMPLE_TAG
DBGI_TAG = DBGI_SAMPLE_TAG
NAL_HEADERS = H264_NAL_HEADER_TYPES


def boxes(data, start=0, end=None):
    """Iterate child boxes; each box size includes its own header (ISO BMFF)."""
    end = len(data) if end is None else end
    p = start
    while p + MP4_BOX_HEADER_SIZE <= end:
        n = struct.unpack_from('>I', data, p)[0]
        typ = data[p + MP4_BOX_SIZE_FIELD_SIZE:p + MP4_BOX_HEADER_SIZE].decode('latin1'); h = MP4_BOX_HEADER_SIZE
        if n == MP4_SIZE_USES_LARGESIZE:
            n = struct.unpack_from('>Q', data, p + MP4_BOX_HEADER_SIZE)[0]; h = MP4_EXTENDED_HEADER_SIZE
        elif n == MP4_SIZE_EXTENDS_TO_EOF: n = end-p
        if n < h or p+n > end: raise ValueError(f'Invalid {typ} box at {p}')
        yield typ, p, n, h; p += n
    if p != end: raise ValueError('Unparsed bytes in box container')


def children(box):
    root = next(iter(boxes(box)))
    return list(boxes(box, root[3], len(box)))


def child(box, name):
    found = [box[p:p+n] for t,p,n,_ in children(box) if t == name]
    if len(found) != 1: raise ValueError(f'Expected one {name} box, found {len(found)}')
    return found[0]


def make_box(name, payload):
    size = MP4_BOX_HEADER_SIZE + len(payload)
    if size > UINT32_MAX: raise ValueError(f'{name} box exceeds 32-bit size')
    return struct.pack('>I4s', size, name.encode('latin1')) + payload


def top_boxes(path):
    size = os.path.getsize(path); result=[]; p=0
    with open(path,'rb') as f:
        while p + MP4_BOX_HEADER_SIZE <= size:
            f.seek(p); h = f.read(MP4_EXTENDED_HEADER_SIZE); n=struct.unpack('>I',h[:MP4_BOX_SIZE_FIELD_SIZE])[0]
            typ=h[MP4_BOX_SIZE_FIELD_SIZE:MP4_BOX_HEADER_SIZE].decode('latin1'); hs=MP4_BOX_HEADER_SIZE
            if n == MP4_SIZE_USES_LARGESIZE: n=struct.unpack('>Q',h[MP4_BOX_HEADER_SIZE:MP4_EXTENDED_HEADER_SIZE])[0]; hs=MP4_EXTENDED_HEADER_SIZE
            elif n == MP4_SIZE_EXTENDS_TO_EOF: n=size-p
            if n<hs or p+n>size: raise ValueError(f'Invalid top-level {typ} box')
            result.append((typ,p,n,hs)); p+=n
    if p!=size: raise ValueError('Invalid top-level box layout')
    return result


def varint(mm,p):
    """Read a bounded base-128 protobuf varint."""
    value=shift=0; start=p
    while True:
        if p>=len(mm): raise ValueError('Truncated metadata varint')
        b=mm[p]; p+=1; value |= (b&0x7f)<<shift
        if not b&0x80: return value,p,p-start
        shift+=7
        if shift > 63:  # protobuf varints are limited to 64 bits here
            raise ValueError('Invalid metadata varint')


def read_csv(path):
    required={'offset','nal_size','frame_index','timestamp','djmd_size','dbgi_size'}
    with open(path,newline='',encoding='utf-8-sig') as f:
        r=csv.DictReader(f)
        if not required.issubset(r.fieldnames or []):
            raise ValueError(f'CSV requires columns: {sorted(required)}')
        rows=list(r)
    rows.sort(key=lambda x:int(x['frame_index']))
    ids=[int(x['frame_index']) for x in rows]
    if not ids or ids != list(range(DJI_FIRST_FRAME_INDEX, len(ids) + DJI_FIRST_FRAME_INDEX)):
        raise ValueError('CSV frame_index values must be consecutive, starting at 1')
    for row in rows:
        for k in required: int(row[k])
    return rows


def extract_samples(source, rows):
    tops = top_boxes(source)
    mdats = [x for x in tops if x[0] == 'mdat']
    if len(mdats) != 1:
        raise ValueError('Source must have exactly one mdat')

    _, mp, ms, mh = mdats[0]
    djmd, dbgi, ts = [], [], []

    if not rows:
        raise ValueError('No validated frame rows supplied')

    with open(source, 'rb') as f, mmap.mmap(
        f.fileno(), 0, access=mmap.ACCESS_READ
    ) as mm:

        # Extract the two raw DJI initialization protobuf blocks.
        payload_start = mp + mh
        first_frame_offset = int(rows[0]['offset'])

        if not (payload_start < first_frame_offset <= mp + ms):
            raise ValueError('Invalid first-frame offset')

        dbgi_signature = b'\x0a\x33\x0a\x25\x0a\x05\x08'
        matches = []
        pos = payload_start

        while True:
            pos = mm.find(dbgi_signature, pos, first_frame_offset)
            if pos < 0:
                break
            matches.append(pos)
            pos += 1

        if len(matches) != 1:
            raise ValueError(
                f'Expected one DBGI initialization signature, found {len(matches)}'
            )

        dbgi_start = matches[0]

        djmd_init = bytes(mm[payload_start:dbgi_start])
        dbgi_init = bytes(mm[dbgi_start:first_frame_offset])

        if not djmd_init or not dbgi_init:
            raise ValueError('Empty DJI initialization block')

        djmd.append(djmd_init)
        dbgi.append(dbgi_init)

        print(
            f"DJI initialization: DJMD={len(djmd_init):,} bytes, "
            f"DBGI={len(dbgi_init):,} bytes"
        )

        # Extract and validate per-frame metadata packets.
        for row in rows:
            frame = int(row['frame_index'])
            no = int(row['offset'])
            nl = int(row['nal_size'])

            do = row.get('djmd_offset', '').strip()
            dp = int(do) if do else no + 4 + nl

            if not (mp + mh <= dp < mp + ms):
                raise ValueError(
                    f'Frame {frame}: DJMD offset outside mdat'
                )

            if mm[dp] != DJMD_TAG:
                raise ValueError(
                    f'Frame {frame}: DJMD tag mismatch at offset {dp}'
                )

            dlen, de, _ = varint(mm, dp + DJI_PROTOBUF_TAG_SIZE)
            if dlen != int(row['djmd_size']):
                raise ValueError(
                    f'Frame {frame}: DJMD length mismatch'
                )

            bp = de + dlen
            if bp >= mp + ms or mm[bp] != DBGI_TAG:
                raise ValueError(
                    f'Frame {frame}: DBGI tag mismatch at offset {bp}'
                )

            blen, be, _ = varint(mm, bp + DJI_PROTOBUF_TAG_SIZE)
            if blen != int(row['dbgi_size']):
                raise ValueError(
                    f'Frame {frame}: DBGI length mismatch'
                )

            if be + blen > mp + ms:
                raise ValueError(
                    f'Frame {frame}: DBGI packet extends beyond mdat'
                )

            djmd.append(bytes(mm[dp:de + dlen]))
            dbgi.append(bytes(mm[bp:be + blen]))
            ts.append(int(row['timestamp']))

    if ts != sorted(ts) or len(set(ts)) != len(ts):
        raise ValueError('Timestamps must be strictly increasing')

    return djmd, dbgi, ts


def fullbox_offsets(box, kind):
    """Return offsets of FullBox fields, accounting for version 0/1 widths.

    Offsets are relative to the beginning of the complete box byte string.
    """
    v = box[MP4_FULLBOX_VERSION_OFFSET]
    if v not in (0,1): raise ValueError(f'Unsupported {kind} version {v}')
    # mdhd: creation_time, modification_time, timescale, duration.
    if kind == 'mdhd': return (MDHD_V0_TIMESCALE_OFFSET, MDHD_V0_DURATION_OFFSET, FULLBOX_V0_FIELD_WIDTH) if v == 0 else (MDHD_V1_TIMESCALE_OFFSET, MDHD_V1_DURATION_OFFSET, FULLBOX_V1_FIELD_WIDTH)
    # tkhd: track_ID and duration follow version-dependent time fields.
    if kind == 'tkhd': return (TKHD_V0_TRACK_ID_OFFSET, TKHD_V0_DURATION_OFFSET, FULLBOX_V0_FIELD_WIDTH) if v == 0 else (TKHD_V1_TRACK_ID_OFFSET, TKHD_V1_DURATION_OFFSET, FULLBOX_V1_FIELD_WIDTH)
    # mvhd: timescale, duration, and next_track_ID.
    if kind == 'mvhd': return (MVHD_V0_TIMESCALE_OFFSET, MVHD_V0_DURATION_OFFSET, MVHD_V0_NEXT_TRACK_ID_OFFSET, FULLBOX_V0_FIELD_WIDTH) if v == 0 else (MVHD_V1_TIMESCALE_OFFSET, MVHD_V1_DURATION_OFFSET, MVHD_V1_NEXT_TRACK_ID_OFFSET, FULLBOX_V1_FIELD_WIDTH)


def track_entry(trak):
    stbl=child(child(child(trak,'mdia'),'minf'),'stbl'); stsd=child(stbl,'stsd')
    if struct.unpack_from('>I', stsd, MP4_STSD_ENTRY_COUNT_OFFSET)[0]<1: raise ValueError('Empty stsd')
    return stsd[MP4_STSD_FIRST_ENTRY_OFFSET:MP4_STSD_FIRST_ENTRY_OFFSET + 4].decode('latin1')


def track_scale(trak):
    mdhd=child(child(trak,'mdia'),'mdhd'); so,do,w=fullbox_offsets(mdhd,'mdhd')
    return struct.unpack_from('>I',mdhd,so)[0]


def reference_durations(trak):
    stts=child(child(child(trak,'mdia'),'minf'),'stbl'); stts=child(stts,'stts')
    count = struct.unpack_from('>I', stts, MP4_STSD_ENTRY_COUNT_OFFSET)[0]; p = MP4_STTS_ENTRIES_OFFSET; runs=[]
    for _ in range(count):
        c,d=struct.unpack_from('>II',stts,p); p += MP4_STTS_ENTRY_SIZE
        if c<1 or d<1: raise ValueError('Invalid reference stts run')
        runs.append((c,d))
    if not runs: raise ValueError('Reference stts is empty')
    return runs


def track_id_duration(trak):
    tkhd=child(trak,'tkhd'); idoff,doff,w=fullbox_offsets(tkhd,'tkhd')
    return struct.unpack_from('>I',tkhd,idoff)[0],doff,w


def stts_box(ds):
    runs=[]
    for d in ds:
        if d<1 or d>UINT32_MAX: raise ValueError('Invalid sample duration')
        if runs and runs[-1][1]==d: runs[-1]=(runs[-1][0]+1,d)
        else: runs.append((1,d))
    return make_box('stts',b'\0'*4+struct.pack('>I',len(runs))+b''.join(struct.pack('>II',*x) for x in runs))


def stsc_box(): return make_box('stsc',b'\0'*4+struct.pack('>IIII',1,1,1,1))


def stsz_box(samples):
    sizes=[len(x) for x in samples]
    return make_box('stsz',b'\0'*4+struct.pack('>II',0,len(sizes))+b''.join(struct.pack('>I',x) for x in sizes))


def offset_box(offsets):
    if max(offsets,default=0)<=UINT32_MAX:
        return make_box('stco',b'\0'*4+struct.pack('>I',len(offsets))+b''.join(struct.pack('>I',x) for x in offsets))
    return make_box('co64',b'\0'*4+struct.pack('>I',len(offsets))+b''.join(struct.pack('>Q',x) for x in offsets))


def replace_children(box,replacements,nested=None):
    root=next(iter(boxes(box))); payload=b''
    for typ,p,n,_ in boxes(box,root[3],len(box)):
        part=box[p:p+n]
        if typ in replacements: part=replacements[typ]
        elif nested and typ in nested: part=nested[typ](part)
        payload+=part
    return make_box(box[MP4_BOX_SIZE_FIELD_SIZE:MP4_BOX_HEADER_SIZE].decode('latin1'),payload)


def rebuild_track(template,newid,tkdur,scale,mdur,samples,durations,offsets):
    repl={'stts':stts_box(durations),'stsc':stsc_box(),'stsz':stsz_box(samples),
          'stco':offset_box(offsets),'co64':offset_box(offsets)}
    def mdia_rebuild(box):
        repl2={}
        for typ,p,n,_ in children(box):
            part=box[p:p+n]
            if typ=='mdhd':
                so,do,w=fullbox_offsets(part,'mdhd'); b=bytearray(part)
                struct.pack_into('>I',b,so,scale); struct.pack_into('>I' if w==4 else '>Q',b,do,mdur)
                repl2[typ]=bytes(b)
            elif typ=='minf':
                repl2[typ]=replace_children(part,{}, {'stbl':lambda x:replace_children(x,repl)})
        return replace_children(box,repl2)
    out=b''
    for typ,p,n,_ in children(template):
        part=template[p:p+n]
        if typ=='tkhd':
            io,do,w=fullbox_offsets(part,'tkhd'); b=bytearray(part)
            struct.pack_into('>I',b,io,newid); struct.pack_into('>I' if w==4 else '>Q',b,do,tkdur); part=bytes(b)
        elif typ=='mdia': part=mdia_rebuild(part)
        out+=part
    return make_box('trak',out)


def verify(path,frame_count):
    ffprobe=shutil.which('ffprobe'); ffmpeg=shutil.which('ffmpeg')
    if not ffprobe or not ffmpeg: raise RuntimeError('Install FFmpeg (ffprobe and ffmpeg) for automatic output verification')
    q=subprocess.run([ffprobe,'-v','error','-show_entries','stream=codec_type,codec_tag_string,time_base,duration,nb_frames','-of','json',str(path)],check=True,text=True,capture_output=True)
    streams=json.loads(q.stdout).get('streams',[])
    tags={s.get('codec_tag_string') for s in streams}
    if not {'djmd','dbgi'}.issubset(tags): raise RuntimeError(f'ffprobe did not find both DJI tracks; tags={tags}')
    videos=[s for s in streams if s.get('codec_type')=='video']
    if not videos: raise RuntimeError('ffprobe found no video stream')
    nf=videos[0].get('nb_frames')
    if nf not in (None,'N/A') and int(nf)!=frame_count: raise RuntimeError(f'Video frames: {nf}, expected {frame_count}')
    d=subprocess.run([ffmpeg,'-v','error','-i',str(path),'-map','0:v:0','-f','null','-'],text=True,capture_output=True)
    if d.returncode: raise RuntimeError('FFmpeg decode failed:\n'+d.stderr)
    print('VERIFY PASS: ffprobe found video + djmd + dbgi; ffmpeg decoded the video stream.')


def metadata_main():
    ap=argparse.ArgumentParser(description='Rebuild DJI djmd/dbgi tracks in a recovered MP4')
    ap.add_argument('--source',required=True,help='Damaged original MP4 containing metadata samples')
    ap.add_argument('--video',required=True,help='Recovered video-only MP4')
    ap.add_argument('--reference',required=True,help='Healthy DJI MP4 containing djmd/dbgi track templates')
    ap.add_argument('--samples-csv',required=True,help='CSV frame offsets, metadata sizes and timestamps')
    ap.add_argument('--output',required=True,help='New MP4 path')
    ap.add_argument('--write',action='store_true',help='Write output; default is a no-write preflight')
    ap.add_argument('--force',action='store_true',help='Permit replacing an existing output')
    a=ap.parse_args(); src,vid,ref,csvp,out=map(Path,(a.source,a.video,a.reference,a.samples_csv,a.output)); tmp=Path(str(out)+'.tmp')
    for x in (src,vid,ref,csvp):
        if not x.is_file(): ap.error(f'Missing input: {x}')
    if not a.force and (out.exists() or tmp.exists()): ap.error(f'Refusing to overwrite {out} or {tmp}; use another name or --force')
    if out.resolve() in {x.resolve() for x in (src,vid,ref,csvp)}: ap.error('Output must not be an input file')

    sb=top_boxes(src); vb=top_boxes(vid); rb=top_boxes(ref)
    sm=[x for x in sb if x[0]=='mdat']; vm=[x for x in vb if x[0]=='mdat']; vmo=[x for x in vb if x[0]=='moov']; rmo=[x for x in rb if x[0]=='moov']
    if len(sm)!=1 or len(vm)!=1 or len(vmo)!=1 or len(rmo)!=1: raise ValueError('Expected one source mdat, video mdat/moov and reference moov')
    _,smp,sms,smh=sm[0]; _,vmp,vms,vmh=vm[0]; _,vop,vos,_=vmo[0]; _,rop,ros,_=rmo[0]
    # PyAV may place other valid top-level boxes (for example 'free') between
    # mdat and moov. Preserve those boxes rather than requiring adjacency.
    if vmh not in (MP4_BOX_HEADER_SIZE, MP4_EXTENDED_HEADER_SIZE):
        raise ValueError(f'Unsupported mdat header size: {vmh}')
    mdat_end=vmp+vms
    if vop < mdat_end: raise ValueError('Video moov must follow the mdat box')
    tail_boxes=[x for x in vb if x[1] >= mdat_end]
    if not any(x[0]=='moov' and x[1]==vop for x in tail_boxes):
        raise ValueError('Could not locate the video moov after mdat')
    print(f"Video mdat: offset={vmp}, size={vms:,} bytes, header={vmh} bytes")
    if vmh == MP4_BOX_HEADER_SIZE and vms > UINT32_MAX:
        raise ValueError('An 8-byte mdat header cannot represent this input size')
    if vmh == MP4_EXTENDED_HEADER_SIZE and vms > UINT64_MAX:
        raise ValueError('Input mdat exceeds the 64-bit size limit')
    rows=read_csv(csvp); dj,db,ts=extract_samples(src,rows)
    with open(vid,'rb') as f: f.seek(vop); vmoov=f.read(vos)
    with open(ref,'rb') as f: f.seek(rop); rmoov=f.read(ros)
    refs={}
    for typ,p,n,_ in children(rmoov):
        if typ=='trak':
            t=rmoov[p:p+n]; entry=track_entry(t)
            if entry in ('djmd','dbgi'):
                if entry in refs: raise ValueError(f'Multiple reference {entry} tracks')
                refs[entry]=t
    if set(refs)!={'djmd','dbgi'}: raise ValueError('Reference must contain djmd and dbgi tracks')
    scales={k:track_scale(v) for k,v in refs.items()}; refd={k:reference_durations(v) for k,v in refs.items()}
    # The first and last sample deltas come from the corresponding reference
    # track's stts table; no recording-specific frame timing constants.
    durations={}
    for k,samples in (('djmd',dj),('dbgi',db)):
        scale=scales[k]; runs=refd[k]; initial=runs[0][1]; final=runs[-1][1]
        pts=[0]+[initial+((t-ts[0])*scale+DJI_TIMESTAMP_TICKS_PER_SECOND//2)//DJI_TIMESTAMP_TICKS_PER_SECOND for t in ts]
        ds=[y-x for x,y in zip(pts,pts[1:])]+[final]
        if any(x<=0 for x in ds): raise ValueError(f'{k} timestamp mapping creates non-positive duration')
        durations[k]=ds

    vchildren=children(vmoov); mvhd=child(vmoov,'mvhd'); tso,dur_o,next_o,mvw=fullbox_offsets(mvhd,'mvhd')
    movie_scale=struct.unpack_from('>I',mvhd,tso)[0]; olddur=struct.unpack_from('>I' if mvw==4 else '>Q',mvhd,dur_o)[0]
    oldtracks=[vmoov[p:p+n] for typ,p,n,_ in vchildren if typ=='trak']
    ids=[track_id_duration(t)[0] for t in oldtracks+list(refs.values())]; tid=max(ids,default=0)+1
    all_durations=[]
    for k in ('djmd','dbgi'):
        mdur=sum(durations[k]); scale=scales[k]
        all_durations.append((mdur*movie_scale+scale//2)//scale)
    # Calculate sample chunk offsets after determining the new mdat start.
    metadata_bytes = sum(map(len, dj)) + sum(map(len, db))
    new_mdat_size = vms + metadata_bytes

    if vmh == MP4_BOX_HEADER_SIZE and new_mdat_size > UINT32_MAX:
        raise ValueError(
            'Enlarged mdat exceeds 32-bit size. '
            'Automatic header expansion would require adjusting video chunk offsets.'
        )

    if vmh == MP4_EXTENDED_HEADER_SIZE and new_mdat_size > UINT64_MAX:
        raise ValueError('Enlarged mdat exceeds 64-bit size')
    # Use IDs above the highest ID already present in the recovered video.
    first_new=max([track_id_duration(t)[0] for t in oldtracks],default=0)+1
    built=[]; cursor=vmp+vms
    for idx,k in enumerate(('djmd','dbgi')):
        samples=dj if k=='djmd' else db; offs=[]
        for sample in samples: offs.append(cursor); cursor+=len(sample)
        mdur=sum(durations[k]); scale=scales[k]; td=(mdur*movie_scale+scale//2)//scale
        built.append(rebuild_track(refs[k],first_new+idx,td,scale,mdur,samples,durations[k],offs))
    newmv=bytearray(mvhd); struct.pack_into('>I' if mvw==4 else '>Q',newmv,dur_o,max([olddur]+all_durations)); struct.pack_into('>I',newmv,next_o,first_new+2)
    outchildren=[]; inserted=False
    for typ,p,n,_ in vchildren:
        part=vmoov[p:p+n]
        if typ=='mvhd': part=bytes(newmv)
        outchildren.append(part)
        if typ=='trak' and not inserted: outchildren.extend(built); inserted=True
    if not inserted: raise ValueError('Recovered video moov has no video trak')
    newmoov=make_box('moov',b''.join(outchildren)); expected=vid.stat().st_size+metadata_bytes+len(newmoov)-vos
    print('Preflight successful')
    print(f'Frames: {len(rows)} | DJMD samples: {len(dj)} | DBGI samples: {len(db)}')
    print(f'DJMD timescale: {scales["djmd"]} | DBGI timescale: {scales["dbgi"]}')
    print(f'DJMD bytes: {sum(map(len,dj))} | DBGI bytes: {sum(map(len,db))} | appended: {metadata_bytes}')
    print(f'New mdat size: {new_mdat_size} | new moov size: {len(newmoov)} | output size: {expected}')
    print(f'Output: {out}')
    if not a.write:
        print('DRY RUN ONLY. Add --write to create output.'); return
    if not a.force and (out.exists() or tmp.exists()): raise FileExistsError('Output appeared during preflight')
    try:
        with open(vid,'rb') as vf,open(tmp,'xb') as dest:
            def copy_range(start,length):
                vf.seek(start); left=length
                while left:
                    block=vf.read(min(COPY_BLOCK,left))
                    if not block: raise IOError('Unexpected end of recovered video')
                    dest.write(block); left-=len(block)
            copy_range(0, vmp)
            if vmh == MP4_BOX_HEADER_SIZE:
                dest.write(struct.pack('>I4s', new_mdat_size, b'mdat'))
            else:
                dest.write(struct.pack('>I4sQ', MP4_SIZE_USES_LARGESIZE, b'mdat', new_mdat_size))
            copy_range(vmp+vmh,vms-vmh)
            for sample in dj: dest.write(sample)
            for sample in db: dest.write(sample)
            # Keep every original top-level box after mdat in its original order,
            # substituting the rebuilt moov. This handles intervening 'free' or
            # other boxes without dropping them.
            for typ,p,n,_ in tail_boxes:
                if typ == 'moov' and p == vop:
                    dest.write(newmoov)
                else:
                    copy_range(p,n)
            dest.flush(); os.fsync(dest.fileno())
        if tmp.stat().st_size!=expected: raise ValueError('Written output size differs from preflight size')
        if out.exists():
            if not a.force: raise FileExistsError(f'Output exists: {out}')
            out.unlink()
        os.link(tmp,out); tmp.unlink()
    except Exception: raise
    print(f'Created {out} ({out.stat().st_size} bytes)')
    # Verification is mandatory after writing: check all three streams and decode
    # the recovered video instead of making the user remember a separate flag.
    verify(out, len(rows))



def read_varint(mm, p, end):
    """Read a protobuf varint without crossing the containing sample boundary."""
    value = 0
    shift = 0
    while p < end and shift <= 63:  # uint64 protobuf varint
        b = mm[p]; p += 1
        value |= (b & 0x7f) << shift
        if not (b & 0x80):
            return value, p
        shift += 7
    raise ValueError('Invalid or truncated varint')


def parse_djmd(mm, start, size):
    end = start + size
    # DJI DJMD sample: field 1 (length-delimited) contains a message with
    # frame number in field 1 and timestamp in field 2 (both varints).
    if size < 2 or mm[start] != PROTOBUF_LENGTH_DELIMITED_FIELD_1: raise ValueError('DJMD outer field')
    n, p = read_varint(mm, start + DJI_PROTOBUF_TAG_SIZE, end)
    inner_end = p + n
    if inner_end > end or p >= inner_end or mm[p] != PROTOBUF_VARINT_FIELD_1: raise ValueError('DJMD frame field')
    frame, p = read_varint(mm, p + DJI_PROTOBUF_TAG_SIZE, inner_end)
    if p >= inner_end or mm[p] != PROTOBUF_VARINT_FIELD_2: raise ValueError('DJMD timestamp field')
    timestamp, p = read_varint(mm, p + DJI_PROTOBUF_TAG_SIZE, inner_end)
    if p > inner_end: raise ValueError('DJMD overflow')
    return frame, timestamp


def parse_dbgi(mm, start, size):
    end = start + size
    # DJI DBGI sample nests a timestamp (field 2) and frame index (field 3).
    if size < 2 or mm[start] != PROTOBUF_LENGTH_DELIMITED_FIELD_1: raise ValueError('DBGI outer field')
    n, p = read_varint(mm, start + DJI_PROTOBUF_TAG_SIZE, end); outer_end = p + n
    if outer_end > end or p >= outer_end or mm[p] != PROTOBUF_LENGTH_DELIMITED_FIELD_1: raise ValueError('DBGI inner field')
    n2, p = read_varint(mm, p + DJI_PROTOBUF_TAG_SIZE, outer_end); inner_end = p + n2
    if inner_end > outer_end or p >= inner_end or mm[p] != PROTOBUF_VARINT_FIELD_2: raise ValueError('DBGI timestamp field')
    timestamp, p = read_varint(mm, p + DJI_PROTOBUF_TAG_SIZE, inner_end)
    if p >= inner_end or mm[p] != PROTOBUF_VARINT_FIELD_3: raise ValueError('DBGI frame field')
    frame, p = read_varint(mm, p + DJI_PROTOBUF_TAG_SIZE, inner_end)
    return frame, timestamp


def find_mdat(path):
    # Works for the damaged file whose mdat extends to EOF (size field zero).
    size = path.stat().st_size
    with open(path, 'rb') as f:
        p = 0
        while p + MP4_BOX_HEADER_SIZE <= size:
            f.seek(p); h = f.read(MP4_EXTENDED_HEADER_SIZE)
            n = struct.unpack('>I', h[:MP4_BOX_SIZE_FIELD_SIZE])[0]; typ = h[MP4_BOX_SIZE_FIELD_SIZE:MP4_BOX_HEADER_SIZE]
            hs = MP4_BOX_HEADER_SIZE
            if n == MP4_SIZE_USES_LARGESIZE:
                if len(h) < MP4_EXTENDED_HEADER_SIZE: raise ValueError('Truncated extended box header')
                n = struct.unpack('>Q', h[MP4_BOX_HEADER_SIZE:MP4_EXTENDED_HEADER_SIZE])[0]; hs = MP4_EXTENDED_HEADER_SIZE
            elif n == MP4_SIZE_EXTENDS_TO_EOF: n = size - p
            if n < hs or p + n > size: raise ValueError(f'Invalid top-level box at {p}')
            if typ == b'mdat': return p, hs, n
            p += n
    raise ValueError('No mdat box found in source')


def scan_source(source):
    """Index validated DJI metadata, then walk H.264 packets sequentially.

    AVCC uses a 4-byte big-endian NAL length followed by that many NAL bytes.
    The prefix is not part of the NAL payload. DJI tags are format-specific.
    """

    mdat_pos, mdat_header, mdat_size = find_mdat(source)
    start = mdat_pos + mdat_header
    end = mdat_pos + mdat_size

    # The containing mdat is the hard upper bound for every NAL, metadata
    # sample, and intervening region. Per-record checks below tighten this
    # further using the next validated DJI metadata boundary.
    mdat_payload_size = end - start

    metadata = {}

    with open(source, 'rb') as f, mmap.mmap(
        f.fileno(), 0, access=mmap.ACCESS_READ
    ) as mm:

        # First index all structurally valid DJMD + DBGI pairs.
        pos = start
        while True:
            dp = mm.find(bytes([DJMD_TAG]), pos, end)
            if dp < 0:
                break
            pos = dp + DJI_PROTOBUF_TAG_SIZE

            try:
                dlen, dstart = read_varint(mm, dp + DJI_PROTOBUF_TAG_SIZE, end)
                dend = dstart + dlen

                if not 1 <= dlen <= mdat_payload_size or dend >= end:
                    continue
                if mm[dend] != DBGI_TAG:
                    continue

                blen, bstart = read_varint(mm, dend + DJI_PROTOBUF_TAG_SIZE, end)
                bend = bstart + blen

                if not 1 <= blen <= mdat_payload_size or bend > end:
                    continue

                df, dt = parse_djmd(mm, dstart, dlen)
                bf, bt = parse_dbgi(mm, bstart, blen)

                if df != bf or dt != bt or df < 1:
                    continue

                record = (dp, dlen, blen, bend, dt)

                if df in metadata and metadata[df] != record:
                    raise ValueError(
                        f"Conflicting metadata records for frame {df}"
                    )

                metadata[df] = record

            except (ValueError, IndexError, struct.error):
                continue

        if not metadata:
            raise ValueError("No valid DJMD/DBGI pairs found")

        frame_ids = sorted(metadata)
        expected_ids = list(range(DJI_FIRST_FRAME_INDEX, len(frame_ids) + DJI_FIRST_FRAME_INDEX))

        if frame_ids != expected_ids:
            raise ValueError(
                "Metadata frame IDs are not consecutive from 1; "
                "refusing ambiguous reconstruction"
            )

        # Determine frame 1's NAL start from its exact DJMD boundary.
        first_djmd = metadata[1][0]
        first_candidates = []

        for p in range(start, first_djmd - AVCC_NAL_LENGTH_SIZE):
            n = struct.unpack_from('>I', mm, p)[0]  # AVCC length prefix is uint32 big-endian

            if not H264_MIN_NAL_UNIT_SIZE <= n <= mdat_payload_size:
                continue

            h = mm[p + AVCC_NAL_LENGTH_SIZE]

            if h not in NAL_HEADERS:
                continue

            if p + AVCC_NAL_LENGTH_SIZE + n == first_djmd:
                first_candidates.append((p, n, h))

        if len(first_candidates) != 1:
            raise ValueError(
                "Could not uniquely establish the first video packet from its "
                "stored AVCC length and the first validated DJMD boundary "
                f"(found {len(first_candidates)} candidates). The scanner will "
                "not guess a start offset or borrow a frame size from the reference. "
                "A damaged first length prefix needs an independently validated "
                "packet-start location before it can be repaired safely."
            )

        cursor = first_candidates[0][0]
        rows = []
        gaps = []

        # Walk strictly in frame order. The end of each DBGI record
        # determines where the next H.264 packet must begin.
        for frame in frame_ids:
            dp, dlen, blen, bend, timestamp = metadata[frame]

            if cursor + AVCC_NAL_LENGTH_SIZE + 1 > dp:
                raise ValueError(
                    f"Frame {frame}: invalid or overlapping packet position"
                )

            n = struct.unpack_from('>I', mm, cursor)[0]
            h = mm[cursor + AVCC_NAL_LENGTH_SIZE]

            if not H264_MIN_NAL_UNIT_SIZE <= n <= mdat_payload_size:
                raise ValueError(
                    f"Frame {frame}: implausible NAL length {n}"
                )

            if h not in NAL_HEADERS:
                raise ValueError(
                    f"Frame {frame}: unexpected NAL header 0x{h:02x}"
                )

            # The stored length counts NAL payload bytes, excluding the
            # AVCC length prefix itself.
            nal_end = cursor + AVCC_NAL_LENGTH_SIZE + n

            if nal_end > dp:
                raise ValueError(
                    f"Frame {frame}: NAL overlaps its DJMD record"
                )

            # A gap can contain DJI auxiliary data; do not silently merge it
            # into the H.264 packet.
            gap = dp - nal_end

            if gap:
                # Do not treat arbitrary bytes as part of H.264. The one
                # known DJI auxiliary block begins with this marker. If a gap
                # does not match it, stop rather than silently skip unknown data.
                if mm[nal_end:nal_end + len(DJI_AUXILIARY_BLOCK_SIGNATURE)] != DJI_AUXILIARY_BLOCK_SIGNATURE:
                    raise ValueError(
                        f"Frame {frame}: {gap:,} unexplained bytes before DJMD; "
                        "not the recognized DJI auxiliary-data block"
                    )
                gaps.append((frame, nal_end, gap))

            rows.append((
                cursor, n, h, frame, timestamp,
                dlen, blen, dp
            ))

            # The next frame begins after this frame's DBGI sample.
            cursor = bend

        timestamps = [r[4] for r in rows]

        if any(b <= a for a, b in zip(timestamps, timestamps[1:])):
            raise ValueError("Timestamps are not strictly increasing")

        counts = {
            "Validated DJMD/DBGI pairs": len(metadata),
            "Sequential H.264 packets": len(rows),
            "Frames with intervening data": len(gaps),
        }

        print(f"First video packet offset: {rows[0][0]:,}")
        print(f"Last DBGI end offset: {cursor:,}")

        for frame, offset, size in gaps:
            print(
                f"Intervening data: frame {frame}, "
                f"offset {offset:,}, size {size:,} bytes"
            )

        return rows, (mdat_pos, mdat_header, mdat_size), counts

def write_csv(rows, path):
    # Existing metadata muxer accepts this CSV schema; djmd_offset records gap cases.
    with open(path, 'x', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(['offset','nal_size','nal_header','frame_index','timestamp','djmd_size','dbgi_size','djmd_offset'])
        for r in rows:
            w.writerow([r[0],r[1],r[2],r[3],r[4],r[5],r[6],r[7]])


def build_video(source, reference, rows, output):
    """Rebuild H.264 packets using a reference template, or write video-only.

    Full recovery uses DJI presentation timestamps (microseconds) and the
    healthy reference for the codec/sample-entry configuration. Without a
    reference, FFmpeg remuxes Annex-B H.264 and estimates a constant frame rate
    from the median DJI timestamp interval; this fallback is approximate.
    """
    if output.exists():
        raise FileExistsError(f"Refusing to overwrite {output}")

    timestamps = [row[4] for row in rows]
    if len(timestamps) > 1:
        import statistics
        median_delta = int(statistics.median(
            later - earlier for earlier, later in zip(timestamps, timestamps[1:])
        ))
        if median_delta <= 0:
            raise ValueError("Cannot derive a positive frame interval from DJI timestamps")
    else:
        # A single frame provides no measurable interval. Use the common
        # 50-fps O3 setting only for this one-frame fallback case.
        median_delta = DJI_TIMESTAMP_TICKS_PER_SECOND // 50

    if reference is None:
        print("No reference supplied: creating video-only output.")
        print("Timing is approximate because raw H.264 does not store MP4 PTS values.")
        raw = output.with_suffix(".recovery_raw.h264")
        if raw.exists():
            raise FileExistsError(f"Refusing to overwrite temporary raw stream: {raw}")
        try:
            with open(source, "rb") as source_file, open(raw, "xb") as raw_file:
                with mmap.mmap(source_file.fileno(), 0, access=mmap.ACCESS_READ) as mm:
                    for row in rows:
                        offset, nal_size = row[0], row[1]
                        nal_start = offset + AVCC_NAL_LENGTH_SIZE
                        raw_file.write(b"\\x00\\x00\\x00\\x01")
                        raw_file.write(mm[nal_start:nal_start + nal_size])

            ffmpeg = shutil.which("ffmpeg")
            if not ffmpeg:
                raise RuntimeError("ffmpeg is required for no-reference video-only mode")
            # Express the estimated rate as a rational, avoiding a rounded
            # integer FPS when the DJI timestamp interval is fractional.
            frame_rate = f"{DJI_TIMESTAMP_TICKS_PER_SECOND}/{median_delta}"
            result = subprocess.run(
                [
                    ffmpeg, "-v", "error", "-y", "-fflags", "+genpts",
                    "-framerate", frame_rate, "-f", "h264", "-i", str(raw),
                    "-c:v", "copy", str(output),
                ],
                capture_output=True, text=True,
            )
            if result.returncode:
                raise RuntimeError("ffmpeg raw-H.264 remux failed: " + result.stderr)
        finally:
            raw.unlink(missing_ok=True)

        verify_video_only(output)
        return

    try:
        import av
    except ImportError:
        raise RuntimeError(
            "PyAV is required for full recovery. Install with: python -m pip install av"
        )

    # Use microseconds as the packet clock because that is the unit of the
    # DJI timestamps being copied into PTS/DTS. The reference supplies the
    # H.264 codec configuration; its track timescale is used for DJI tracks.
    from fractions import Fraction
    time_base = Fraction(1, DJI_TIMESTAMP_TICKS_PER_SECOND)
    reference_container = av.open(str(reference))
    output_container = av.open(
        str(output), mode="w", format="mp4",
        options={"video_track_timescale": str(DJI_TIMESTAMP_TICKS_PER_SECOND)},
    )
    try:
        reference_stream = next(
            (stream for stream in reference_container.streams if stream.type == "video"),
            None,
        )
        if reference_stream is None:
            raise ValueError("Reference contains no video stream")

        output_stream = output_container.add_stream_from_template(reference_stream)
        output_stream.time_base = time_base
        last_duration = median_delta
        first_timestamp = timestamps[0]

        with open(source, "rb") as source_file:
            with mmap.mmap(source_file.fileno(), 0, access=mmap.ACCESS_READ) as mm:
                for index, row in enumerate(rows):
                    offset, nal_size, nal_header = row[0], row[1], row[2]
                    stored_size = struct.unpack_from(">I", mm, offset)[0]
                    if stored_size != nal_size or mm[offset + AVCC_NAL_LENGTH_SIZE] != nal_header:
                        raise ValueError(f"NAL changed or invalid at source offset {offset}")

                    nal_start = offset + AVCC_NAL_LENGTH_SIZE
                    nal_payload = mm[nal_start:nal_start + nal_size]
                    packet_data = nal_size.to_bytes(AVCC_NAL_LENGTH_SIZE, "big") + nal_payload
                    packet = av.Packet(packet_data)
                    packet.pts = packet.dts = timestamps[index] - first_timestamp
                    packet.duration = (
                        timestamps[index + 1] - timestamps[index]
                        if index + 1 < len(timestamps) else last_duration
                    )
                    packet.time_base = time_base
                    packet.stream = output_stream
                    output_container.mux(packet)

                    if (index + 1) % 1000 == 0:
                        print(f"  Reconstructed {index + 1:,}/{len(rows):,} video packets")
    finally:
        output_container.close()
        reference_container.close()


def verify_video_only(path):
    """Check that FFmpeg can decode the video-only fallback."""
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise RuntimeError("Install FFmpeg (ffmpeg and ffprobe) to verify the output")
    probe = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries",
         "stream=codec_type,codec_name,width,height,nb_frames,duration",
         "-of", "json", str(path)],
        check=True, text=True, capture_output=True,
    )
    streams = json.loads(probe.stdout).get("streams", [])
    if not any(stream.get("codec_type") == "video" for stream in streams):
        raise RuntimeError("ffprobe found no video stream in the video-only output")
    decode = subprocess.run(
        [ffmpeg, "-v", "error", "-i", str(path), "-map", "0:v:0", "-f", "null", "-"],
        text=True, capture_output=True,
    )
    if decode.returncode:
        raise RuntimeError("Video-only output failed FFmpeg decode:\\n" + decode.stderr)
    print("VERIFY PASS: ffprobe found a video stream and FFmpeg decoded it.")



def main():
    parser = argparse.ArgumentParser(
        description="Recover video and DJI metadata tracks from a damaged DJI O3 MP4"
    )
    parser.add_argument(
        "source", type=Path,
        help="Damaged original MP4; the source is never modified",
    )
    parser.add_argument(
        "--reference", type=Path,
        help="Healthy MP4 from the same DJI device/configuration; needed for full DJI-track recovery",
    )
    parser.add_argument(
        "--output", type=Path, default=Path("recovered_dji.mp4"),
        help="Output MP4 path (default: recovered_dji.mp4)",
    )
    parser.add_argument(
        "--video-only", action="store_true",
        help="Create a video-only MP4 when no healthy reference is available",
    )
    args = parser.parse_args()

    if not args.source.is_file():
        parser.error(f"Source not found: {args.source}")
    if args.reference and not args.reference.is_file():
        parser.error(f"Reference not found: {args.reference}")
    if args.reference and args.video_only:
        parser.error("Choose either full recovery with --reference or --video-only without it")
    if not args.reference and not args.video_only:
        parser.error(
            "Full DJI metadata-track recovery requires --reference; "
            "otherwise specify --video-only"
        )

    input_paths = {args.source.resolve()}
    if args.reference:
        input_paths.add(args.reference.resolve())
    if args.output.resolve() in input_paths:
        parser.error("Output must not overwrite an input file")
    if args.output.exists():
        parser.error(f"Refusing to overwrite existing output: {args.output}")

    rows, mdat, counts = scan_source(args.source)
    print("Scan summary:")
    for label, value in counts.items():
        print(f"  {label}: {value:,}")
    print(f"  Validated frames: {len(rows):,} (frame IDs {rows[0][3]}–{rows[-1][3]})")

    if args.reference:
        with tempfile.TemporaryDirectory(prefix="dji_recovery_") as temporary_directory:
            temporary_directory = Path(temporary_directory)
            samples_csv = temporary_directory / "samples.csv"
            video_mp4 = temporary_directory / "video_only.mp4"

            write_csv(rows, samples_csv)
            print("Reconstructing video packets...")
            build_video(args.source, args.reference, rows, video_mp4)

            # Invoke the metadata-track builder with its internal arguments.
            # The user-facing interface stays intentionally small.
            sys.argv = [
                "recover_dji_mp4.py",
                "--source", str(args.source),
                "--video", str(video_mp4),
                "--reference", str(args.reference),
                "--samples-csv", str(samples_csv),
                "--output", str(args.output),
                "--write",
            ]
            metadata_main()
    else:
        print("WARNING: no-reference mode cannot rebuild DJI metadata tracks.")
        build_video(args.source, None, rows, args.output)
        print(f"Created video-only output: {args.output} ({args.output.stat().st_size:,} bytes)")


if __name__ == '__main__':
    try: main()
    except (ValueError,OSError,struct.error,subprocess.CalledProcessError,RuntimeError) as e:
        print(f'ERROR: {e}',file=sys.stderr); sys.exit(1)

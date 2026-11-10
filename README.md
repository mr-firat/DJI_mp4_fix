# DJI_mp4_fix

This project was developed with technical research, reverse-engineering assistance, debugging support, and documentation assistance from ChatGPT by OpenAI.

Recovers the video and the DJI metadata tracks (`djmd`, `dbgi`) from a damaged
DJI O3 MP4, such as a recording cut off by a power loss or crash that has no
`moov` box.

The script reads the damaged file without changing it, finds every frame in
the `mdat` payload, and writes a new playable MP4. It never overwrites an
input or an existing output.

## Requirements

- Python 3.8 or newer
- FFmpeg (`ffmpeg` and `ffprobe` on `PATH`), used to verify the output and for
  video-only mode
- [PyAV](https://pyav.org/), needed for full recovery only:

  ```sh
  python -m pip install av
  ```

## Usage

### Full recovery (video + DJI metadata tracks)

You need a healthy reference file recorded by the same device with the same
settings (resolution, frame rate, codec). The reference provides the H.264
codec configuration and the `djmd`/`dbgi` track templates.

```sh
python DJI_mp4_fix.py DJI_0081.MP4 --reference DJI_0080.MP4 --output recovered.mp4
```

### Video-only recovery (no reference)

```sh
python DJI_mp4_fix.py DJI_0081.MP4 --video-only --output recovered_video_only.mp4
```

This mode cannot rebuild the DJI metadata tracks. Frame timing is approximate:
the frame rate is estimated from the median interval between DJI timestamps,
and the output is treated as constant frame rate.

### Options

| Argument | Description |
|---|---|
| `source` | Damaged MP4 (positional, required). Read only. |
| `--reference PATH` | Healthy MP4 from the same device/configuration. Required for full recovery. |
| `--video-only` | Produce a video-only MP4 without a reference. Cannot be combined with `--reference`. |
| `--output PATH` | Output path. Default: `recovered_dji.mp4`. Must not exist yet. |

You must pass either `--reference` or `--video-only`.

## How it works

1. **Locate `mdat`.** Walks the top-level boxes of the damaged file. An `mdat`
   whose size field is `0` (extends to end of file) is supported, which is the
   usual state of an unfinalized recording.
2. **Index DJI metadata.** Scans `mdat` for `DJMD` (tag `0x1A`) records each
   followed directly by a `DBGI` (tag `0x12`) record. A pair is kept only if both
   protobuf payloads parse and agree on frame number and timestamp. Frame
   numbers must run consecutively from 1.
3. **Find the first video packet.** Searches before frame 1's `DJMD` for the
   one AVCC packet (4-byte big-endian length + H.264 NAL) that ends exactly at
   that `DJMD` boundary. If there is not exactly one candidate, the script stops
   rather than guess.
4. **Walk frames in order.** Each frame is laid out as
   `[H.264 NAL][optional DJI auxiliary block][DJMD][DBGI]`, and the end of each
   `DBGI` is where the next NAL starts. Every NAL length and header byte is
   checked. A gap between a NAL and its `DJMD` is accepted only when it starts
   with the known DJI auxiliary signature `44 33 22 11`; any other gap stops
   the run.
5. **Rebuild the video.**
   - *Full:* PyAV muxes each NAL with PTS/DTS taken from the DJI timestamps
     (microseconds), using the reference's video stream as the codec template.
   - *Video-only:* the NALs are written as an Annex-B `.h264` stream and FFmpeg
     remuxes it into MP4 at the estimated frame rate.
6. **Add the DJI tracks (full mode).** The `djmd` and `dbgi` initialization
   blocks and per-frame samples are appended to the new `mdat`. Fresh `trak`
   boxes are built from the reference templates with new `stts`, `stsc`,
   `stsz` and `stco`/`co64` tables. Sample timing comes from the DJI
   timestamps, and the first and last durations come from the reference
   track's `stts`. `mvhd` duration and `next_track_ID` are updated.
7. **Write safely.** The output is first written to `<output>.tmp`
   (exclusive create), fsynced, and its size compared with the size predicted
   during preflight. It is then hard-linked to the final name.
8. **Verify.** `ffprobe` must report a video stream plus `djmd` and `dbgi`
   tracks (full mode) and the expected frame count. `ffmpeg` must then decode
   the whole video stream without errors.

## Example output

```
First video packet offset: 1,234
Last DBGI end offset: 1,597,452,000
Intervening data: frame 4, offset 5,678,901, size 1,929,848 bytes
Scan summary:
  Validated DJMD/DBGI pairs: 12,345
  Sequential H.264 packets: 12,345
  Frames with intervening data: 1
  Validated frames: 12,345 (frame IDs 1–12345)
Reconstructing video packets...
  Reconstructed 1,000/12,345 video packets
  ...
Preflight successful
...
Created recovered.mp4 (1595799178 bytes)
VERIFY PASS: ffprobe found video + djmd + dbgi; ffmpeg decoded the video stream.
```

The numbers above are illustrative.

## Limitations

- Built for DJI O3 recordings. The DJI tags, protobuf field numbers and the
  auxiliary-block signature were observed in O3 files and are not part of the
  MP4 standard. Other DJI devices may use different layouts.
- Only H.264 is handled, with 4-byte AVCC length prefixes and the NAL header
  bytes `0x21 0x25 0x41 0x45 0x61 0x65`. H.265/HEVC recordings are not supported.
- Audio is not recovered.
- The damaged file must contain exactly one `mdat`.
- Full recovery fails if the enlarged `mdat` would exceed 4 GiB while the
  rebuilt video uses a 32-bit `mdat` header.
- The script stops on the first inconsistency (gaps in frame numbers,
  unrecognized bytes between frames, a damaged first length prefix) instead of
  skipping data. Files whose damage is inside `mdat`, and not just a missing
  `moov`, may therefore not be recoverable with it.

## Exit status

`0` on success. On any validation, I/O or verification error it prints
`ERROR: <message>` to stderr and exits with `1`.

## References

- ISO/IEC 14496-12, ISO Base Media File Format (MP4 box layout)
- ISO/IEC 14496-15, AVC file format (AVCC length-prefixed NAL units)


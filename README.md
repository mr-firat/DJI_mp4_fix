# DJI_mp4_fix

Repair DJI O3 videos that won't play because the recording was cut off, for
example by a battery disconnect, crash or power loss.

The tool rebuilds a playable MP4 with the video and DJI's flight metadata
tracks (`djmd`, `dbgi`). It then prints a **damage report** that says what was
wrong with the file and what was fixed.

Your original file is never modified, and the tool never overwrites an
existing file.

## What you need

- **The damaged video**, the one that won't play.
- **A healthy video** recorded with the **same drone/camera and the same
  settings** (resolution, frame rate). Another clip from the same flight works
  well. The tool copies the video format settings from this file; none of its
  picture content is used.
- Python 3.8 or newer.
- [FFmpeg](https://ffmpeg.org/download.html), installed so that `ffprobe` is
  on your `PATH`. Check by running `ffprobe -version`.

## Setup (one time)

Open a terminal in this folder. Create a virtual environment and install
PyAV:

```sh
python3 -m venv av-env
source av-env/bin/activate        # Windows: av-env\Scripts\activate
python -m pip install av
```

In each new terminal, activate the environment again before using the tool.

## Repair a video

```sh
python DJI_mp4_fix.py damaged.MP4 --reference healthy.MP4 --output repaired.mp4
```

Replace `damaged.MP4` and `healthy.MP4` with your own file names. A long
recording can take a minute or two. When it finishes, open `repaired.mp4` and
read the damage report at the end of the output.

| Option | What it does |
|---|---|
| `damaged.MP4` | The video to repair (required). |
| `--reference FILE` | A healthy video from the same drone/settings (required). |
| `--output FILE` | Where to save the repaired video. Default: `recovered_dji.mp4`. The file must not exist yet. |
| `--index-csv FILE` | Optional. Also save a per-frame table (offsets, sizes, timestamps, repairs) for analysis. |

## What it can and can't fix

| Problem | Result | What happens |
|---|---|---|
| Recording cut off, file won't open (missing `moov` index) | ✅ Fixed | The index is rebuilt from the frames in the file. |
| File size information is wrong or missing | ✅ Fixed | The tool uses the data that is actually there. |
| File ends in the middle of a frame | ✅ Fixed | Every complete frame is recovered; only the cut-off last frame is lost. |
| Half-written data at the end of the file | ✅ Fixed | Ignored and reported. |
| Large recordings (repaired file over 4 GiB) | ✅ Supported | A 64-bit size field is used when needed. |
| A frame's size field is damaged | ✅ Fixed | The correct size is calculated from the surrounding data. The frame is fully recovered. |
| The first frame is damaged | ✅ Fixed | Found from the data in front of it, then repaired, or dropped if it can't be repaired. |
| DJI setup data at the start of the file is damaged | ✅ Fixed | Recovered as long as the first frame is intact. |
| A frame's header or metadata is damaged | ⚠️ Partly | Only that frame is dropped; the rest of the video is recovered. |
| Bit errors inside the picture data | ⚠️ Partly | The frame is kept and listed in the report. It may show a glitch until the next keyframe. |
| Bit errors in metadata values (e.g. orientation) | ⚠️ Partly | Copied as-is and not detected. Damaged frame numbers and timestamps *are* detected. |
| The DJI setup data **and** the first frame are both damaged | ❌ No | The start of the video can't be found reliably, so the tool stops instead of guessing. |
| The damaged picture itself | ❌ No | Damaged frames are reported, not restored. |
| Other DJI cameras, H.265 videos, audio | ❌ No | Only DJI O3 H.264 recordings are supported. Audio is not recovered. |

Small amounts of damage never make the tool give up; it recovers everything it
can and reports the rest.

## Reading the damage report

Example from a real crashed recording, with extra damage added for testing:

```
=== Damage report ===
Problems found:
  - mdat size field is 0: the recording was never finalized
  - moov box is missing: the file has no index and cannot be played
  - 27,789 bytes after the last complete frame at offset 1,597,424,499 not recovered (frame 10094 cut off: NAL length 101,632, 27,785 bytes present)
  - Decoder found picture damage in 1 frame(s): 500. Expect visual glitches there, possibly until the next keyframe
Repairs and skipped data:
  - Frame 1: NAL length 999,999 at offset 12,187 was invalid; corrected to 126,154 from the metadata boundary
Notes:
  - 1 frame(s) carry a DJI auxiliary block (normal); it is not copied
  - Recovered 10,093 of 10,093 frames
```

- **Problems found:** what was wrong with the file.
- **Repairs and skipped data:** what the tool corrected or had to drop.
- **Notes:** a summary, including how many frames were recovered and which
  were lost.

If something goes wrong, the tool prints `ERROR: <reason>` and the report of
what it found so far. It exits with status `1`, or `0` on success.

## How it works

For those interested in the details:

1. Finds the media data (`mdat`) in the damaged file, tolerating a wrong or
   missing size.
2. Indexes every DJI metadata pair (`DJMD` + `DBGI`) whose frame number and
   timestamp agree.
3. Finds frame 1 by reading through DJI's setup data at the start of `mdat`,
   a sequence of protobuf fields. It is cross-checked against frame 1's size
   field.
4. Walks the frames in order. Each frame is
   `[H.264 video][optional DJI aux block][DJMD][DBGI]` and starts where the
   previous frame's `DBGI` ends. Damaged frames are repaired or dropped.
5. Rebuilds the video with PyAV, using DJI's timestamps and the codec
   settings of the healthy reference.
6. Adds the DJI metadata tracks, using the reference's tracks as templates.
7. Checks the result with `ffprobe` and decodes every frame to find picture
   damage.

## Credits

- Original script: developed with technical research, reverse-engineering,
  debugging and documentation assistance from ChatGPT by OpenAI.
- Refactor, damage repair/reporting and this README: Claude Opus 5.5 by
  Anthropic, via Claude Code.

## References

- ISO/IEC 14496-12, ISO Base Media File Format (MP4 box layout)
- ISO/IEC 14496-15, AVC file format (AVCC length-prefixed NAL units)

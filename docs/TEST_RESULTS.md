# Test results

Aggregate numbers from one real library, processed between September and November 2026. They record what happened in this test and aren't a promise of what you'll get. A library of already well-compressed files could save far less, and one of high-bitrate files could save more.

## Setup

- Two Windows desktops: one main PC and one worker
- Files stored on a network share
- `libx265`, CRF 20, preset `medium`, with a minimum VMAF of 95
- Video was re-encoded; audio and subtitles were copied unchanged

The software was developed during this period, so settings and bug fixes changed along the way. Earlier mistakes in the code caused some files to be retried, and those files appear here under their final outcome.

## Outcomes

| Outcome | Files | Share |
|---|---:|---:|
| Converted and replaced | 2,271 | 41.2% |
| Left alone: no size gain | 2,817 | 51.1% |
| Skipped: format not selected or already HEVC | 398 | 7.2% |
| Failed quality verification | 3 | 0.05% |
| Still queued or in progress | 23 | 0.4% |
| **Total** | **5,512** | |

## Space

| | |
|---|---|
| All files analyzed | 5.02 TB |
| Converted files, before | 2.75 TB |
| Converted files, after | 1.68 TB |
| **Saved** | **1.07 TB** |
| Saved, as a share of converted files | 39.0% |
| Saved, as a share of everything analyzed | 21.4% |
| Video runtime converted | about 1,025 hours |

Size reduction per converted file varied widely. The median was 29.2%, and the middle 80% of files fell between 6.0% and 58.2%.

The 2,817 "no size gain" files came to 1.52 TB and were left unchanged. They tended to have lower bitrates already: a median of 2.8 Mbps, against 4.1 Mbps for files that converted.

## Quality scores

VMAF scores for converted files, compared with their sources. The score had to reach 95 for a file to be replaced, so no lower scores can appear here.

| | |
|---|---|
| Mean | 97.45 |
| Median | 97.53 |
| Lowest | 95.00 |
| Highest | 99.86 |

| Final CRF | Files |
|---:|---:|
| 20 (default) | 2,204 |
| 17 (retried after a low score) | 59 |
| 14 (retried twice) | 3 |
| Not recorded | 5 |

## By source format

| Format | Analyzed | Converted | No size gain | Skipped | Failed | Saved | Median reduction |
|---|---:|---:|---:|---:|---:|---:|---:|
| H.264 | 5,069 | 2,238 | 2,806 | 0 | 3 | 1,061.8 GB | 29.1% |
| HEVC | 328 | 0 | 0 | 328 | 0 | - | - |
| WMV3 | 60 | 0 | 0 | 60 | 0 | - | - |
| MPEG-4 (DivX/Xvid) | 25 | 18 | 6 | 0 | 0 | 4.5 GB | 40.3% |
| VC-1 | 15 | 10 | 5 | 0 | 0 | 4.7 GB | 29.0% |
| WMV2 | 10 | 0 | 0 | 10 | 0 | - | - |
| MPEG-1 | 5 | 5 | 0 | 0 | 0 | 3.5 GB | 42.3% |

Rows may not add up to "Analyzed" because files still in the queue aren't in any outcome column. Nearly all of the test was H.264, so the small samples of other formats say little.

WMV2 and WMV3 are deliberately not converted: small test samples of both grew instead of shrinking.

## By source resolution

| Resolution | Analyzed | Converted | No size gain | Saved | Median reduction |
|---|---:|---:|---:|---:|---:|
| 2160p and up | 556 | 74 | 160 | 67.0 GB | 29.9% |
| 1080p | 3,310 | 1,410 | 1,874 | 794.3 GB | 23.7% |
| 720p | 952 | 437 | 493 | 163.3 GB | 38.0% |
| Under 720p | 694 | 350 | 290 | 49.8 GB | 29.3% |

## By file type

| Extension | Analyzed | Converted |
|---|---:|---:|
| `.mp4` | 5,403 | 2,243 |
| `.wmv` | 85 | 10 |
| `.avi` | 12 | 8 |
| `.mpg` | 4 | 4 |
| `.mkv` | 4 | 3 |
| `.mov` | 3 | 3 |
| `.flv` | 1 | 0 |

## Two PCs sharing the work

| PC | Files converted |
|---|---:|
| Worker | 1,412 |
| Main | 689 |
| Not recorded (before per-PC tracking was added) | 170 |

The split reflects when each PC was running and how fast it was, so it doesn't measure one PC against the other.

## Limits of this test

- It covers one library from one person, on two PCs.
- **It contained no HDR files**, so HDR handling is untested.
- Quality was judged by the automated checks, not by watching every file. VMAF is a good measure but can't catch everything.
- Results were measured on these encoder settings. Different CRF or preset values will change them.

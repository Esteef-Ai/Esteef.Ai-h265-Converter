# Esteef.Ai h265 Converter

Batch-converts a video library to H.265 (HEVC) on Windows, unattended, and checks every result before touching the original. Point it at a folder, and it works through the files one at a time from a web dashboard. Other PCs on your network can help with the workload.

The app and its files are named AIConverter, which is the name you'll see on the dashboard and in the commands below.

**Replacing a file only happens after it passes all of these checks:**

- It decodes from start to finish with no errors.
- Its resolution matches the source exactly.
- Its duration matches the source within 1 second.
- It scores at least 95 on [VMAF](https://github.com/Netflix/vmaf), a perceptual quality metric, compared with the source.

Files that wouldn't get smaller are left alone. Originals are kept for 5 days after replacement, and you can restore them from the dashboard during that time.

> **This tool deletes original files.** Once the 5-day holding period ends, the original is gone for good. VMAF is a measurement, not a guarantee that you won't see a difference. Keep backups of anything you can't replace, try it on a small test folder first, and spot-check the results. The software comes with no warranty; see [LICENSE](LICENSE).

## Test experience

These figures come from one person's library, run over about eight weeks in autumn 2026 on two Windows desktops. They describe what happened in that test and are **not a prediction of your results**. Your savings depend on how your files were encoded in the first place. The software was also still being developed during the test, so not every file went through the final version.

| | |
|---|---|
| Files analyzed | 5,512 (5.02 TB) |
| Converted and replaced | 2,271 (41.2%) |
| Left alone: no size gain | 2,817 (51.1%) |
| Skipped: format not selected or already HEVC | 398 (7.2%) |
| Failed quality verification | 3 (0.05%) |
| Still queued when these numbers were taken | 23 (0.4%) |
| **Space saved** | **1.07 TB** |

Saved space was 39.0% of the size of the converted files (2.75 TB down to 1.68 TB), or 21.4% of everything analyzed. More than half of the files did not get smaller, so the second figure is the more realistic picture of a whole library.

See [docs/TEST_RESULTS.md](docs/TEST_RESULTS.md) for the breakdown by format and resolution, quality scores and limits of the test.

## What you need

- **Windows 10 or 11.** Windows only.
- **[Python](https://www.python.org/downloads/) 3.10 or newer.** Tick "Add python.exe to PATH" in the installer.
- **ffmpeg 7.1 or newer** with `libx265` and `libvmaf`, on your PATH. It isn't included. The test used a Windows "gpl" build (n7.1.1) from [BtbN/FFmpeg-Builds](https://github.com/BtbN/FFmpeg-Builds/releases). Unzip it and add its `bin` folder to your PATH. `setup.ps1` tells you if your build is missing anything.
- **Free disk space on the PC doing the work.** Each file is copied there while it's encoded, and the queue pauses if C: drops below 15 GB.

Encoding is slow and uses every CPU core available. A full encode and quality check can take longer than the video itself runs.

## Setup

1. Download this repository and put it in a folder such as `C:\AIConverter`.
2. Open PowerShell in that folder and run:

   ```powershell
   powershell -ExecutionPolicy Bypass -File setup.ps1
   ```

3. Double-click **`Start AIConverter.bat`**. The dashboard opens at http://localhost:8765.
4. Click **Browse...**, choose a folder, and click **Add directory**. Conversion starts right away.

To stop it, double-click **`Stop AIConverter.bat`**. A file that was in progress starts over the next time it runs.

To start it automatically when you log in (optional):

```powershell
powershell -ExecutionPolicy Bypass -File install_startup.ps1
```

If Windows Update restarts the PC, the converter won't run again until someone logs in. Setting Windows Update's active hours helps avoid that.

## Using more than one PC

Other PCs can take files from the same queue, as long as they can all reach the files over a network share. See [docs/MULTIPLE_MACHINES.md](docs/MULTIPLE_MACHINES.md).

## How it works

1. **Scan.** Watched folders are checked when added and every 30 minutes after that, so new files get picked up. Each folder is set to convert H.264 only, or H.264 plus older formats (MPEG-1, VC-1, DivX/Xvid).
2. **Encode.** The source is copied to local disk, then encoded with `libx265` at CRF 20, preset `medium`. Audio, subtitles, chapters and metadata are copied as they are. Resolution and frame rate don't change.
3. **Give up early when it won't help.** Once an encode is at least 25% done, its final size is projected. If it would end up more than 3% larger than the source, the encode stops and the file is marked "no size gain".
4. **Verify.** The checks at the top of this page run. If only the VMAF score falls short, the file is retried at a lower CRF, provided the result could still come out smaller. Other failures are retried at the same CRF, up to 3 attempts.
5. **Replace.** The new file is copied next to the original. The original is renamed to `<name>.aiconverter-trash`, and the new file takes its name. Containers that can't hold HEVC (such as `.avi` and `.wmv`) become `.mkv`.
6. **Purge.** After 5 days the `.aiconverter-trash` file is deleted. Until then, the **Quarantine** page can restore it.

The queue is stored in a local SQLite database, so it picks up where it left off after a crash or restart. A watchdog restarts the app if it hangs, for example on a network drive that stops responding.

## Settings

`config.json` is created the first time the app runs. Edit it and restart to change anything. The settings you'd most likely change:

| Setting | Default | What it does |
|---|---|---|
| `crf` | `20` | Encode quality. Lower is higher quality and larger files. |
| `preset` | `medium` | x265 speed/compression tradeoff. |
| `min_vmaf_score` | `95.0` | Minimum score needed to replace a file. |
| `quarantine_days` | `5` | How long originals are kept after replacement. |
| `work_dir` | `work` in the app folder | Local folder used while encoding. |
| `min_free_c_drive_gb` | `15` | Pause the queue below this much free space on C:. |

Every setting is listed with an explanation in [aiconverter/config.py](aiconverter/config.py).

`config.json`, the `data` folder and the `logs` folder hold your file paths and any keys. Don't share or commit them. `.gitignore` already excludes them.

## Known limits

- **HDR is untested.** The test library had no HDR files. Basic color tags are copied, but HDR10 mastering metadata isn't, so check HDR files yourself.
- **Incomplete or damaged files** are logged and skipped until the file changes.
- **CPU encoding only.** There is no GPU encoding.
- **The dashboard has no login.** By default only the PC running it can reach it. See the warning in [docs/MULTIPLE_MACHINES.md](docs/MULTIPLE_MACHINES.md) before opening it to your network.

## License

[MIT](LICENSE). FFmpeg and x265 are separate projects under their own licenses and aren't included here. If you redistribute an ffmpeg build along with this, its license (GPL for the builds above) applies to it.

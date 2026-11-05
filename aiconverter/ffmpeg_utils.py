"""All direct interaction with ffmpeg/ffprobe: probing source files, running
the actual x265 encode with progress reporting, and the post-encode quality
verification pass (VMAF + duration/frame sanity checks)."""
import json
import os
import re
import subprocess
import threading
import time

from . import control
from .config import CONFIG

_PRIORITY_FLAGS = {
    "normal": 0,
    "below_normal": subprocess.BELOW_NORMAL_PRIORITY_CLASS,
    "idle": subprocess.IDLE_PRIORITY_CLASS,
}


def _kwargs():
    return dict(
        encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW | _PRIORITY_FLAGS.get(CONFIG.get("process_priority", "normal"), 0),
    )


def _run(cmd, timeout, cwd=None):
    """subprocess.run() equivalent whose timeout only counts time the
    process was actually allowed to run -- time spent frozen by a pause
    (see control.pause_now) doesn't count, so pausing a worker for an
    evening of gaming doesn't turn into a spurious "VMAF timed out" failure.
    The process is also registered with control so pause/stop reach it."""
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd, **_kwargs())
    control.register(proc)
    active = 0.0
    step = 2.0
    try:
        while True:
            try:
                out, err = proc.communicate(timeout=step)
                break
            except subprocess.TimeoutExpired:
                if not control.is_frozen():
                    active += step
                if active > timeout:
                    proc.kill()
                    proc.communicate()
                    raise subprocess.TimeoutExpired(cmd, timeout)
    finally:
        control.unregister(proc)
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)

HDR_TRANSFERS = {"smpte2084", "arib-std-b67"}


class ProbeError(Exception):
    pass


class NoSizeGainDetected(Exception):
    """Raised by encode() when it projects, partway through, that the output
    will end up larger than the source -- lets the caller bail out instead of
    burning the rest of the encode time on a file that was never going to be
    replaced anyway (see CONFIG['no_gain_check_min_pct'] / ['no_gain_margin'])."""
    def __init__(self, projected_size, source_size, at_pct):
        self.projected_size = projected_size
        self.source_size = source_size
        self.at_pct = at_pct
        super().__init__(
            f"Projected output size {projected_size} bytes exceeds source {source_size} bytes "
            f"(projected at {at_pct:.0f}% through the encode)"
        )


def probe(path):
    """Return a dict describing the primary video (and presence of audio/subs)
    stream of a media file, via ffprobe -show_streams -show_format."""
    cmd = [
        CONFIG["ffprobe_bin"], "-v", "error",
        "-print_format", "json",
        "-show_format", "-show_streams",
        path,
    ]
    try:
        result = _run(cmd, CONFIG["subprocess_timeout_probe"])
    except subprocess.TimeoutExpired:
        # A hung ffprobe (e.g. a flaky network/mounted drive that stops
        # responding mid-read) would otherwise block this call forever with
        # no exception -- that previously froze the whole worker thread with
        # nothing to catch.
        raise ProbeError(f"ffprobe timed out after {CONFIG['subprocess_timeout_probe']}s on {path}")
    if result.returncode != 0:
        raise ProbeError(result.stderr.strip() or f"ffprobe failed on {path}")
    data = json.loads(result.stdout)
    streams = data.get("streams", [])
    fmt = data.get("format", {})
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        raise ProbeError(f"No video stream found in {path}")
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    has_subs = any(s.get("codec_type") == "subtitle" for s in streams)

    fps = _parse_rate(video.get("avg_frame_rate") or video.get("r_frame_rate"))
    duration = float(fmt.get("duration") or video.get("duration") or 0.0)
    bitrate = int(fmt.get("bit_rate") or 0)

    return {
        "codec": video.get("codec_name"),
        "width": video.get("width"),
        "height": video.get("height"),
        "fps": fps,
        "duration": duration,
        "size": int(fmt.get("size") or 0),
        "bitrate": bitrate,
        "pix_fmt": video.get("pix_fmt"),
        "color_primaries": video.get("color_primaries"),
        "color_transfer": video.get("color_transfer"),
        "color_space": video.get("color_space"),
        "has_audio": has_audio,
        "has_subs": has_subs,
        "is_hdr": video.get("color_transfer") in HDR_TRANSFERS,
    }


def _parse_rate(rate_str):
    if not rate_str or rate_str == "0/0":
        return None
    if "/" in rate_str:
        num, den = rate_str.split("/")
        den = float(den)
        return round(float(num) / den, 3) if den else None
    return float(rate_str)


# Colour tag values that can be passed straight through to a YUV x265 encode.
_VALID_COLOR_TAGS = {
    "color_primaries": {"bt709", "bt470m", "bt470bg", "smpte170m", "smpte240m", "film", "bt2020",
                        "smpte428", "smpte431", "smpte432", "jedec-p22", "ebu3213"},
    "color_transfer": {"bt709", "gamma22", "gamma28", "smpte170m", "smpte240m", "linear", "log100", "log316",
                       "iec61966-2-4", "bt1361e", "iec61966-2-1", "bt2020-10", "bt2020-12", "smpte2084",
                       "smpte428", "arib-std-b67"},
    "color_space": {"bt709", "fcc", "bt470bg", "smpte170m", "smpte240m", "ycgco", "bt2020nc", "bt2020c",
                    "smpte2085", "chroma-derived-nc", "chroma-derived-c", "ictcp"},
}

_TIME_RE = re.compile(r"out_time_ms=(\d+)")
_PROGRESS_RE = re.compile(r"progress=(\w+)")
_SPEED_RE = re.compile(r"speed=\s*([0-9.]+)x")
_SIZE_RE = re.compile(r"total_size=(\d+)")


def encode(source_path, output_path, source_info, on_progress=None, stall_timeout_s=600, crf=None, source_size=None):
    """Run the actual x265 encode. Streams -progress to a pipe so we can report
    percent-complete and detect a stalled/hung encode (no progress line for
    stall_timeout_s) so the caller can kill and retry it.

    on_progress, if given, is called as on_progress(pct, speed) where speed is
    ffmpeg's own self-reported encode speed as a multiple of realtime (e.g.
    0.35 means it's taking ~3x longer than the video's own runtime) -- this is
    what the queue manager uses to estimate time remaining.

    crf overrides CONFIG["crf"] for this one encode -- used when a job is
    being retried at a lower (higher-quality) CRF after failing verification
    at the default setting.

    source_size, if given, enables an early-abort check: once far enough into
    the encode for the projection to be reliable (CONFIG['no_gain_check_min_pct']),
    the current bytes-written-so-far is extrapolated to a projected final size
    (bytes_so_far / fraction_done). If that projection clearly exceeds
    source_size (CONFIG['no_gain_margin']), the encode is killed immediately
    and NoSizeGainDetected is raised -- there's no point spending the rest of
    the encode time on a file that was never going to be smaller anyway."""
    effective_crf = CONFIG["crf"] if crf is None else crf
    args = [
        CONFIG["ffmpeg_bin"], "-y", "-nostdin",
        # Ignore container crop metadata (MP4/MOV "clap"/tkhd): ffmpeg 7.1+
        # applies it on decode, most players don't, and it made outputs come
        # out a different size than the source (e.g. 1280x720 -> 1248x702).
        # "codec", not "0": the codec's own crop must still apply -- 1080p
        # H.264 is coded as 1088 lines and cropped to 1080 ("0" broke that).
        "-apply_cropping", "codec", "-i", source_path,
        "-map", "0",
        # Drop data tracks (e.g. a camera timecode 'tmcd' track). They hold
        # no picture/sound, and MP4 often can't carry them through a
        # re-encode -- ffmpeg then refuses to start ("Could not find tag for
        # codec none").
        "-map", "-0:d",
        # Keep every source frame with its original timestamp -- no frames
        # dropped or duplicated to force a constant frame rate. Many
        # files have slightly irregular timestamps (and occasional gaps);
        # without this, ffmpeg silently drops/dupes frames, the output is no
        # longer frame-for-frame identical to the source, and VMAF ends up
        # comparing mismatched frames (scores of 1-60 on fine encodes).
        "-fps_mode", "passthrough",
        "-c:v", CONFIG["video_codec"],
        "-crf", str(effective_crf),
        "-preset", CONFIG["preset"],
        "-c:a", "copy", "-c:s", "copy",
        "-map_metadata", "0", "-map_chapters", "0",
    ]
    if CONFIG["encoder_threads"]:
        args += ["-threads", str(CONFIG["encoder_threads"])]
    # Preserve color characteristics explicitly -- x265 does not auto-copy
    # these from the source the way some encoders do, and getting this wrong
    # silently shifts the picture (washed out colors, wrong gamma) which is
    # exactly the kind of "not actually one-to-one" regression we want to avoid.
    #
    # Only values ffmpeg/x265 accept for a YUV encode are passed on. Some
    # sources carry bogus tags -- e.g. colorspace "gbr" (an RGB label) on an
    # ordinary YUV video, which makes x265 refuse to start at all. An unusable tag is dropped (output left untagged, which
    # players treat as the normal default) rather than failing the file.
    for key, flag in (("color_primaries", "-color_primaries"), ("color_transfer", "-color_trc"), ("color_space", "-colorspace")):
        val = source_info.get(key)
        if val and val in _VALID_COLOR_TAGS[key]:
            args += [flag, val]
    args += ["-progress", "pipe:1", "-nostats", output_path]

    # stderr is sent to a file rather than a second PIPE: reading only stdout
    # in a loop below while stderr also fills its own pipe buffer is a classic
    # Windows subprocess deadlock (ffmpeg blocks writing to stderr, so it never
    # writes more progress to stdout either, and this loop waits forever).
    stderr_path = output_path + ".stderr.log"
    duration = source_info.get("duration") or 0
    pct = 0.0
    speed = None

    min_pct = CONFIG["no_gain_check_min_pct"]
    margin = CONFIG["no_gain_margin"]

    with open(stderr_path, "w", encoding="utf-8", errors="replace") as stderr_f:
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=stderr_f, **_kwargs())
        control.register(proc)

        # The stall check below only runs when a stdout line actually
        # arrives -- if ffmpeg hangs completely and NEVER produces a single
        # line (e.g. stuck reading from an unresponsive network/mounted
        # drive), `for line in proc.stdout` blocks forever with that check
        # never reached. This independent thread catches exactly that case:
        # it does nothing but sleep and compare timestamps, so it isn't
        # vulnerable to the same I/O hang it's watching for.
        watchdog_state = {"last": time.time(), "killed_for_stall": False}
        stop_watchdog = threading.Event()

        def _stall_watchdog():
            while not stop_watchdog.wait(5):
                if control.is_frozen():
                    # Frozen on purpose by a pause -- not a stall.
                    watchdog_state["last"] = time.time()
                    continue
                if time.time() - watchdog_state["last"] > stall_timeout_s:
                    watchdog_state["killed_for_stall"] = True
                    proc.kill()
                    return

        watchdog_thread = threading.Thread(target=_stall_watchdog, name="encode-stall-watchdog", daemon=True)
        watchdog_thread.start()

        try:
            for line in proc.stdout:
                m = _TIME_RE.search(line)
                if m and duration:
                    out_time_s = int(m.group(1)) / 1_000_000
                    pct = min(100.0, round(100 * out_time_s / duration, 1))
                    watchdog_state["last"] = time.time()
                sm = _SPEED_RE.search(line)
                if sm:
                    speed = float(sm.group(1))
                sizem = _SIZE_RE.search(line)
                if sizem and source_size and pct >= min_pct:
                    bytes_so_far = int(sizem.group(1))
                    projected = bytes_so_far / (pct / 100)
                    if projected > source_size * margin:
                        proc.kill()
                        raise NoSizeGainDetected(int(projected), source_size, pct)
                if m or sm or _PROGRESS_RE.search(line):
                    watchdog_state["last"] = time.time()
                    if on_progress:
                        on_progress(pct, speed)
            if watchdog_state["killed_for_stall"]:
                raise TimeoutError(f"ffmpeg stalled for over {stall_timeout_s}s on {source_path}")
        finally:
            stop_watchdog.set()
            control.unregister(proc)
            # If we're leaving early (an exception raised mid-loop, e.g. the
            # job was handed back to the queue), don't sit waiting for the
            # rest of a multi-hour encode to finish on its own.
            if proc.poll() is None:
                proc.kill()
            proc.wait()

    try:
        with open(stderr_path, "r", encoding="utf-8", errors="replace") as f:
            stderr_text = f.read()
        os.remove(stderr_path)
    except OSError:
        stderr_text = ""

    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg exited {proc.returncode}: {stderr_text[-2000:]}")
    # ffmpeg exits 0 even when reading the input failed part-way -- it skips
    # ahead and keeps going, leaving gaps in the output. Treat that as a
    # failed encode rather than trusting the result.
    for marker in ("Error during demuxing", "I/O error", "Input/output error", "Error reading"):
        if marker in stderr_text:
            line = next((l for l in stderr_text.splitlines() if marker in l), marker)
            raise RuntimeError(f"Reading the source failed during the encode ({line.strip()[:200]})")


def decode_check(path):
    """Cheap completeness check: fully decode the file with no output, error
    on any decode error. Much faster than a full VMAF pass and catches
    truncated/corrupt files (e.g. from an interrupted copy)."""
    cmd = [CONFIG["ffmpeg_bin"], "-v", "error", "-nostdin", "-i", path, "-f", "null", "-"]
    try:
        result = _run(cmd, CONFIG["subprocess_timeout_decode_check"])
    except subprocess.TimeoutExpired:
        return False, f"decode_check timed out after {CONFIG['subprocess_timeout_decode_check']}s"
    return result.returncode == 0, result.stderr.strip()


_VMAF_SCORE_RE = re.compile(r'"mean":\s*([0-9.]+)')


def vmaf_compare(reference_path, distorted_path, width, height):
    """Compare the re-encoded file against the original using libvmaf. Scales
    both to a common comparison resolution and runs the comparison. Returns
    the mean VMAF score (0-100).

    Two cost-reduction knobs, both standard practice for batch verification
    rather than research-grade measurement:
      - vmaf_max_height caps the comparison resolution (e.g. 4K sources score
        at 1080p instead of native 4K -- ~4x fewer pixels). Resolution match
        itself is already verified separately via ffprobe in queue_manager,
        so this only affects how finely VMAF samples detail, not whether the
        output is actually the right resolution.
      - vmaf_n_subsample scores every Nth frame instead of every frame. The
        mean score over a well-distributed sample stays representative; the
        main thing lost is sensitivity to one very brief, localized dip.

    libvmaf's own feature-scoring pass does NOT parallelize automatically --
    left at its default it runs near-single-threaded regardless of how many
    cores the machine has, which is the dominant cost for a large 4K file (far
    slower per-frame than the decode itself). n_threads is set explicitly to
    use every logical core for this step."""
    log_filename = os.path.basename(distorted_path) + ".vmaf.json"
    work_dir = os.path.dirname(os.path.abspath(distorted_path))
    log_path = os.path.join(work_dir, log_filename)
    vmaf_threads = CONFIG["vmaf_threads"] or (os.cpu_count() or 4)

    max_height = CONFIG["vmaf_max_height"]
    if max_height and height > max_height:
        cmp_height = max_height
        cmp_width = round(width * max_height / height / 2) * 2  # keep even for yuv420p
    else:
        cmp_height = height
        cmp_width = width

    n_subsample = CONFIG["vmaf_n_subsample"] or 1
    # log_path is a value inside a filtergraph option string, which uses ':' as
    # a key/value separator -- a bare Windows path ("C:/...") breaks that
    # parser no matter how it's quoted/escaped. Running with cwd=work_dir and
    # passing just the bare filename (no drive letter, no colon) sidesteps the
    # problem entirely instead of fighting libavfilter's escaping rules.
    # Frames are paired by POSITION (1st with 1st, 2nd with 2nd...), not by
    # timestamp: both streams are re-stamped as 0,1,2... in the same
    # timebase. Pairing by original timestamps went wrong whenever the
    # source's timestamps jittered (half-tick rounding on 59.94fps rips
    # etc.), comparing each frame against its neighbour and reporting
    # VMAF ~55 (or even ~1) for encodes that were actually fine.
    pair = "settb=1/1000,setpts=N"
    filter_complex = (
        f"[0:v]scale={cmp_width}:{cmp_height}:flags=bicubic,{pair}[dist];"
        f"[1:v]scale={cmp_width}:{cmp_height}:flags=bicubic,{pair}[ref];"
        f"[dist][ref]libvmaf=log_fmt=json:log_path={log_filename}:n_threads={vmaf_threads}:n_subsample={n_subsample}[vmafout]"
    )
    # -reinit_filter 0: never rebuild the filter graph mid-file. ffmpeg
    # rebuilds it whenever a stream's frame properties change partway
    # through (e.g. colour tags switching from "unknown" to bt709 where two
    # clips were joined) -- and a rebuild restarts setpts' frame counter N
    # at 0, so every later frame got compared against the wrong one (job
    # 602: VMAF 6.6 with the rebuild, 96.6 without). scale= absorbs any
    # such change, so a single graph for the whole file is safe.
    cmd = [
        CONFIG["ffmpeg_bin"], "-v", "error", "-nostdin",
        "-threads", str(vmaf_threads), "-reinit_filter", "0",
        "-i", distorted_path,
        "-threads", str(vmaf_threads), "-reinit_filter", "0", "-apply_cropping", "codec",
        "-i", reference_path,
        "-filter_complex", filter_complex,
        # Explicitly map ONLY the libvmaf filter's output. Without this,
        # ffmpeg auto-maps both inputs' audio tracks into the same null
        # output too, and their independent/overlapping timestamps corrupt
        # the whole pipeline -- this was the actual cause of wildly wrong
        # VMAF scores, not threading or subsampling.
        "-map", "[vmafout]", "-an", "-sn",
        "-f", "null", "-",
    ]
    try:
        result = _run(cmd, CONFIG["subprocess_timeout_vmaf"], cwd=work_dir)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"VMAF comparison timed out after {CONFIG['subprocess_timeout_vmaf']}s")
    if result.returncode != 0:
        raise RuntimeError(f"VMAF comparison failed: {result.stderr.strip()[-2000:]}")
    with open(log_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    os.remove(log_path)
    return data["pooled_metrics"]["vmaf"]["mean"]

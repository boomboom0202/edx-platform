"""Recordings on disk: appending pieces, making files seekable, serving them in ranges."""
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path

from django.http import FileResponse, HttpResponse

from .conf import conf

log = logging.getLogger(__name__)


class ChunkTooLarge(Exception):
    pass


def root():
    return Path(conf("ROOT"))


def free_mb():
    try:
        root().mkdir(parents=True, exist_ok=True)
        return shutil.disk_usage(root()).free // (1024 * 1024)
    except OSError:
        return 0


def relative_dir(session):
    return f"{session.created:%Y/%m}/{session.key}"


def absolute(path):
    return root() / path


def append(recording, stream, limit):
    """
    Append one piece to the recording's file and return how many bytes were added.

    The caller holds a row lock on the recording, so pieces never interleave.
    """
    path = absolute(recording.path)
    path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with open(path, "ab") as target:
        # A request that died half-way may have left a tail the database never counted.
        if target.tell() != recording.size:
            target.truncate(recording.size)
            target.seek(recording.size)
        while True:
            piece = stream.read(65536)
            if not piece:
                break
            written += len(piece)
            if written > limit:
                target.truncate(recording.size)
                raise ChunkTooLarge()
            target.write(piece)
    return written


def ffmpeg():
    try:
        import imageio_ffmpeg  # pylint: disable=import-outside-toplevel
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # pylint: disable=broad-except
        return shutil.which("ffmpeg")


def finalize(recording):
    """
    Rewrite a finished recording with a duration and an index.

    The browser streams WebM without them, and a player cannot jump around in
    such a file. Without ffmpeg the file stays as it is and still plays from the start.
    """
    path = absolute(recording.path)
    exe = ffmpeg()
    if recording.seekable or not exe or not path.exists() or not path.stat().st_size:
        return False
    fixed = path.with_suffix(".fixed.webm")
    try:
        result = subprocess.run(
            [exe, "-y", "-v", "error", "-stats", "-i", str(path), "-c", "copy", "-f", "webm", str(fixed)],
            capture_output=True, text=True, timeout=600, check=False,
        )
        if result.returncode != 0 or not fixed.exists() or not fixed.stat().st_size:
            log.warning("ffmpeg could not rewrite %s: %s", path, result.stderr[-500:])
            fixed.unlink(missing_ok=True)
            return False
        times = re.findall(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)", result.stderr)
        if times:
            hours, minutes, seconds = times[-1]
            recording.duration_ms = int((int(hours) * 3600 + int(minutes) * 60 + float(seconds)) * 1000)
        os.replace(fixed, path)
    except (OSError, subprocess.SubprocessError):
        log.exception("Could not rewrite %s", path)
        fixed.unlink(missing_ok=True)
        return False
    recording.size = path.stat().st_size
    recording.seekable = True
    recording.save(update_fields=["size", "seekable", "duration_ms"])
    return True


def delete_files(session):
    for folder in {absolute(recording.path).parent for recording in session.recordings.all()}:
        shutil.rmtree(folder, ignore_errors=True)


def ranged_response(request, path, content_type):
    """
    Serve part of a file.

    A ranged response never carries more than MEDIA_RESPONSE_BYTES: the player
    asks for the next part itself, and no web worker is tied up by a slow viewer.
    """
    size = path.stat().st_size
    match = re.match(r"bytes=(\d*)-(\d*)$", request.META.get("HTTP_RANGE", "").strip())
    if not match or not (match.group(1) or match.group(2)):
        response = FileResponse(open(path, "rb"), content_type=content_type)  # pylint: disable=consider-using-with
        response["Content-Length"] = size
    else:
        if match.group(1):
            start = int(match.group(1))
            end = int(match.group(2)) if match.group(2) else size - 1
        else:
            start, end = max(size - int(match.group(2)), 0), size - 1
        if start >= size:
            response = HttpResponse(status=416)
            response["Content-Range"] = f"bytes */{size}"
            return response
        end = min(end, size - 1, start + conf("MEDIA_RESPONSE_BYTES") - 1)
        with open(path, "rb") as source:
            source.seek(start)
            response = HttpResponse(source.read(end - start + 1), content_type=content_type, status=206)
        response["Content-Range"] = f"bytes {start}-{end}/{size}"
    response["Accept-Ranges"] = "bytes"
    response["Cache-Control"] = "private, no-store"
    return response

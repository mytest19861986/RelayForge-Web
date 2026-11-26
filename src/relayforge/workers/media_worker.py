"""Local media renderer. Its only IPC endpoint is a private AF_UNIX socket."""

import base64
import io
import json
import os
import shutil
import signal
import socketserver
import subprocess
import sys
import threading
import wave
from pathlib import Path
from time import monotonic
from uuid import UUID

from PIL import Image, ImageDraw, ImageFont, ImageOps

from relayforge.core.media_validation import IMAGE_TYPES, MAX_BYTES, media_format
from relayforge.core.persian_text_render import visual_text

MAX_RPC = 48 * 1024 * 1024
COLORS = ("#172554", "#ffffff")
STREAM_FORMATS = {"WAV": "wav", "OGG": "ogg", "MP3": "mp3", "MP4": "mov"}
FORBIDDEN_MEDIA = (b"ffconcat", b"#extm3u", b"http://", b"https://", b"tcp://", b"file://")


def confined(root, name):
    path = Path(name)
    if not path.parts or path.is_absolute() or ".." in path.parts or "\0" in name:
        raise ValueError("INVALID_STAGING_PATH")
    cursor = root
    for part in path.parts:
        cursor /= part
        if cursor.is_symlink():
            raise ValueError("INVALID_STAGING_PATH")
    if not cursor.resolve().is_relative_to(root.resolve()) or not cursor.is_file():
        raise ValueError("INVALID_STAGING_PATH")
    return cursor


def input_bytes(root, name, allowed):
    path = confined(root, name)
    with path.open("rb") as stream:
        data = stream.read(MAX_BYTES + 1)
    if any(marker in data.lower() for marker in FORBIDDEN_MEDIA):
        raise ValueError("FORBIDDEN_MEDIA_PROTOCOL")
    kind, _ = media_format(data)
    if kind not in allowed:
        raise ValueError("MEDIA_FORMAT_INVALID")
    return path, data, kind


def infographic(spec, output):
    rows = spec.get("rows", [])
    if not isinstance(rows, list) or len(rows) > 18:
        raise ValueError("INVALID_INFOGRAPHIC")
    palette = spec.get("visual_tokens", {}) if spec.get("ownership_attested") is True else {}
    bg = palette.get("background", COLORS[0])
    fg = palette.get("foreground", COLORS[1])
    canvas = Image.new("RGB", (960, 155 + len(rows) * 56), bg)
    draw = ImageDraw.Draw(canvas)
    font = ImageFont.truetype("DejaVuSans.ttf", 30, layout_engine=ImageFont.Layout.BASIC)
    labels = [(spec.get("title", ""), "")] + rows
    for index, (key, value) in enumerate(labels):
        y = 44 + index * 56
        for text, x in ((key, 912), (value, 450)):
            draw.text((x, y), visual_text(str(text)[:120]), font=font, fill=fg, anchor="ra")
    canvas.save(output)


def sticker(data, native, output):
    with Image.open(io.BytesIO(data)) as source:
        image = ImageOps.contain(source.convert("RGBA"), (512, 512))
    canvas = Image.new("RGBA", (512, 512))
    canvas.paste(image, ((512 - image.width) // 2, (512 - image.height) // 2))
    canvas.save(output, **({"quality": 82, "method": 4} if native else {}))
    if native and output.stat().st_size >= 512 * 1024:
        raise ValueError("STICKER_TOO_LARGE")


def _child(root, command, spec):
    if command == "render_infographic":
        infographic(spec, generated_path(root, "result.png"))
    else:
        _, data, _ = input_bytes(root, spec["source"], IMAGE_TYPES)
        native = spec.get("native") is True
        sticker(data, native, generated_path(root, "result.webp" if native else "result.png"))


class Job:
    def __init__(self):
        self.lock = threading.Lock()
        self.child = None
        self.cancelled = False
        self.deadline = None

    def kill(self):
        if self.child is not None and self.child.poll() is None:
            try:
                os.killpg(self.child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def cancel(self):
        with self.lock:
            self.cancelled = True
            self.kill()

    def run(self, args, wall_time, stdin=None):
        with self.lock:
            if self.cancelled:
                raise ValueError("CANCELLED")
            if self.deadline is None:
                self.deadline = monotonic() + wall_time
            remaining = self.deadline - monotonic()
            if remaining <= 0:
                raise ValueError("TIMED_OUT")
            self.child = subprocess.Popen(
                args,
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stdin=stdin if stdin is not None else subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        try:
            status = self.child.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            with self.lock:
                self.kill()
            self.child.wait()
            raise ValueError("TIMED_OUT") from None
        with self.lock:
            if self.cancelled:
                raise ValueError("CANCELLED")
            if status in {-9, 137}:
                raise ValueError("OOM_KILLED")
            if status:
                raise ValueError("RENDER_FAILED")


class MediaServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True

    def __init__(self, socket_path, staging):
        self.staging = staging.resolve(strict=True)
        self.jobs = {}
        self.lock = threading.Lock()
        super().__init__(socket_path, Handler, bind_and_activate=False)
        try:
            self.server_bind()
            os.chmod(socket_path, 0o660 if os.getenv("RF_MEDIA_SHARED_GROUP") == "true" else 0o600)
            self.server_activate()
        except BaseException:
            self.server_close()
            raise

    def command(self, request):
        command = request["command"]
        if command == "ping":
            return {"status": "READY", "ffmpeg": shutil.which("ffmpeg") is not None}
        identifier = str(UUID(request["job_id"]))
        if command == "cancel_job":
            with self.lock:
                job = self.jobs.get(identifier)
                if job is None:
                    return {"status": "NOT_RUNNING"}
                job.cancel()
            return {"status": "CANCELLED"}
        if command not in {
            "render_infographic",
            "build_sticker",
            "compose_clip_ffmpeg",
            "assemble_podcast_audio",
        }:
            raise ValueError("UNKNOWN_COMMAND")
        spec = request.get("spec", {})
        if not isinstance(spec, dict):
            raise ValueError("INVALID_SPEC")
        wall_time = min(120, max(1, int(request.get("timeout_sec", 60))))
        with self.lock:
            if identifier in self.jobs:
                raise ValueError("ALREADY_RUNNING")
            self.jobs[identifier] = job = Job()
        root = self.staging / identifier
        try:
            if root.is_symlink() or not root.is_dir():
                raise ValueError("INVALID_STAGING_PATH")
            if command in {"render_infographic", "build_sticker"}:
                with confined_spec(root, spec) as config:
                    job.run(
                        [
                            sys.executable,
                            "-m",
                            "relayforge.workers.media_worker",
                            str(root),
                            command,
                        ],
                        wall_time,
                        stdin=config,
                    )
                name = (
                    "result.webp"
                    if command == "build_sticker" and spec.get("native") is True
                    else "result.png"
                )
                duration = None
            else:
                name, duration = transcode(root, command, spec, job, wall_time)
            with confined(root, name).open("rb") as stream:
                data = stream.read(MAX_BYTES + 1)
            kind, size = media_format(data)
            return {
                "status": "SUCCEEDED",
                "format": kind,
                "dimensions": size,
                "duration_sec": duration,
                "byte_size": len(data),
                "content_base64": base64.b64encode(data).decode("ascii"),
            }
        finally:
            try:
                if root.is_dir() and not root.is_symlink():
                    shutil.rmtree(root)
            finally:
                with self.lock:
                    del self.jobs[identifier]


def confined_spec(root, spec):
    import tempfile

    stream = tempfile.TemporaryFile(dir=root)
    try:
        stream.write(json.dumps(spec, ensure_ascii=False).encode())
        stream.seek(0)
    except BaseException:
        stream.close()
        raise
    return stream


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        self.request.settimeout(125)
        try:
            raw = self.rfile.readline(MAX_RPC + 1)
            if not raw.endswith(b"\n") or len(raw) > MAX_RPC:
                raise ValueError("INVALID_RPC_LENGTH")
            response = self.server.command(json.loads(raw))
        except (KeyError, TypeError, ValueError, OSError) as error:
            code = str(error)
            if not code.isascii() or not code.replace("_", "").isalnum() or not code.isupper():
                code = "INVALID_MEDIA_REQUEST"
            response = {"status": code}
        self.wfile.write(json.dumps(response, separators=(",", ":")).encode() + b"\n")


def _ffmpeg():
    executable = shutil.which("ffmpeg")
    if executable is None:
        raise ValueError("FFMPEG_UNAVAILABLE")
    return [executable, "-nostdin", "-y", "-protocol_whitelist", "file,pipe"]


def generated_path(root, name):
    path = root / name
    if path.exists() or path.is_symlink():
        raise ValueError("INVALID_STAGING_PATH")
    return path


def transcode(root, command, spec, job, wall_time):
    ffmpeg = _ffmpeg()
    sources = spec["sources"]
    if not isinstance(sources, list) or not 1 <= len(sources) <= 48:
        raise ValueError("INVALID_MEDIA_SOURCES")
    if command == "compose_clip_ffmpeg":
        return _clip(root, spec, sources, job, wall_time, ffmpeg)
    args = list(ffmpeg)
    for source in sources:
        path, _, kind = input_bytes(root, source, {"WAV", "MP3", "OGG", "MP4"})
        args.extend(("-f", STREAM_FORMATS[kind], "-i", str(path)))
    filters = [
        f"[{index}:a:0]aresample=48000,aformat=channel_layouts=mono[a{index}]"
        for index in range(len(sources))
    ]
    mix = "".join(f"[a{index}]" for index in range(len(sources)))
    filters.append(f"{mix}concat=n={len(sources)}:v=0:a=1,loudnorm=I=-16:TP=-1.5:LRA=11[out]")
    wav = generated_path(root, "normalized.wav")
    job.run(
        args + ["-filter_complex", ";".join(filters), "-map", "[out]", "-ar", "48000", str(wav)],
        wall_time,
    )
    with wave.open(str(wav), "rb") as audio:
        duration = audio.getnframes() / audio.getframerate()
    kind = spec.get("output_format", "VOICE_NOTE")
    if kind == "AUDIO":
        return wav.name, duration
    if kind != "VOICE_NOTE":
        raise ValueError("MEDIA_FORMAT_INVALID")
    opus = generated_path(root, "result.ogg")
    job.run(ffmpeg + ["-f", "wav", "-i", str(wav), "-c:a", "libopus", str(opus)], wall_time)
    return opus.name, duration


def _clip(root, spec, sources, job, wall_time, ffmpeg):
    frames = []
    for index, source in enumerate(sources):
        _, data, _ = input_bytes(root, source, IMAGE_TYPES)
        with Image.open(io.BytesIO(data)) as image:
            frame = ImageOps.pad(image.convert("RGB"), (640, 360))
        caption = spec.get("caption")
        if caption:
            font = ImageFont.truetype("DejaVuSans.ttf", 22, layout_engine=ImageFont.Layout.BASIC)
            ImageDraw.Draw(frame).text(
                (620, 310), visual_text(caption[:100]), font=font, anchor="ra"
            )
        path = generated_path(root, f"rf-slide-{index:02d}.png")
        frame.save(path)
        frames.append(path)
    output = generated_path(root, "result.mp4")
    args = ffmpeg + ["-f", "image2", "-framerate", "1/2", "-i", str(root / "rf-slide-%02d.png")]
    audio = spec.get("audio")
    if audio:
        path, _, kind = input_bytes(root, audio, {"WAV", "MP3", "OGG", "MP4"})
        args += ["-f", STREAM_FORMATS[kind], "-i", str(path), "-c:a", "aac"]
    duration = len(frames) * 2
    job.run(
        args
        + [
            "-vf",
            "fps=24",
            "-c:v",
            "mpeg4",
            "-pix_fmt",
            "yuv420p",
            "-t",
            str(duration),
            "-movflags",
            "+faststart",
            str(output),
        ],
        wall_time,
    )
    return output.name, duration


def main():
    if len(sys.argv) == 3:
        _child(Path(sys.argv[1]), sys.argv[2], json.load(sys.stdin))
        return
    socket_path = os.environ["RF_MEDIA_SOCKET_PATH"]
    staging = Path(os.environ["RF_MEDIA_STAGING_ROOT"])
    with MediaServer(socket_path, staging) as server:
        try:
            server.serve_forever()
        finally:
            Path(socket_path).unlink(missing_ok=True)


if __name__ == "__main__":
    main()

import io
import struct
import zlib

import pytest
from PIL import Image

from relayforge.core.media_queries import pronunciation, suggest_query
from relayforge.core.media_validation import media_format
from relayforge.core.persian_text_render import visual_text
from relayforge.workers.media_worker import confined, infographic, input_bytes, sticker


def test_stock_minimization_and_persian_pronunciation():
    assert suggest_query("طبیعت علی 09123456789 user@example.com @private_id") == "nature"
    assert suggest_query("اسم ناشناس شماره ۱۲۳۴۵") == "abstract"
    assert pronunciation("۱۲۳۴۵ تومان") == "دوازده هزار و سیصد و چهل و پنج تومان"
    assert "۱۲۳۴۵" in visual_text("مبلغ 12345 تومان")


def test_infographic_and_sticker(tmp_path):
    rows = {"title": "گزارش", "rows": [("قیمت", "۱۲۳۴۵")]}
    plain, denied, allowed = [tmp_path / f"{name}.png" for name in ("plain", "denied", "allowed")]
    infographic(rows, plain)
    colors = {"visual_tokens": {"background": "#00aa55"}}
    infographic(rows | colors, denied)
    infographic(rows | colors | {"ownership_attested": True}, allowed)
    assert plain.read_bytes() == denied.read_bytes() != allowed.read_bytes()
    assert media_format(plain.read_bytes()) == ("PNG", (960, 211))
    with Image.open(plain) as image:
        assert len(image.getcolors(image.width * image.height)) > 1
    rendered = tmp_path / "sticker.webp"
    sticker(plain.read_bytes(), True, rendered)
    assert rendered.stat().st_size < 512 * 1024
    with Image.open(rendered) as image:
        assert (image.format, image.size, image.mode) == ("WEBP", (512, 512), "RGBA")
    regular = tmp_path / "sticker.png"
    sticker(plain.read_bytes(), False, regular)
    assert media_format(regular.read_bytes())[0] == "PNG"


def test_magic_bomb_and_staging_guards(tmp_path):
    fake = tmp_path / "fake.jpg"
    fake.write_bytes(b"\x7fELF executable")
    with pytest.raises(ValueError, match="MEDIA_FORMAT_INVALID"):
        media_format(fake.read_bytes())
    chunk = b"IHDR" + struct.pack("!IIBBBBB", 10000, 4001, 8, 2, 0, 0, 0)
    bomb = (
        b"\x89PNG\r\n\x1a\n" + struct.pack("!I", 13) + chunk + struct.pack("!I", zlib.crc32(chunk))
    )
    with pytest.raises(ValueError, match="INVALID_IMAGE"):
        media_format(bomb)
    (tmp_path / "link").symlink_to(fake)
    for name in ("../fake.jpg", "link", str(fake)):
        with pytest.raises(ValueError, match="INVALID_STAGING_PATH"):
            confined(tmp_path, name)
    for index, text in enumerate((b"ffconcat version 1.0", b"#EXTM3U", b"https://example.test")):
        path = tmp_path / f"bad{index}"
        path.write_bytes(text)
        with pytest.raises(ValueError, match="FORBIDDEN_MEDIA_PROTOCOL"):
            input_bytes(tmp_path, path.name, {"MP4"})
    with io.BytesIO() as stream:
        Image.new("RGB", (2, 2)).save(stream, format="PNG")
        assert media_format(stream.getvalue()) == ("PNG", (2, 2))


def test_synthetic_image_speech_and_async_video_transports():
    import base64
    import json
    import wave

    import httpx

    from relayforge.adapters.ai_gateway import GeminiHttpAdapter, OpenAiHttpAdapter
    from relayforge.adapters.ai_media import synthetic_probe

    png = io.BytesIO()
    Image.new("RGB", (5, 3), "blue").save(png, format="PNG")
    wav = io.BytesIO()
    with wave.open(wav, "wb") as output:
        output.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
        output.writeframes(b"\x00\x00" * 20)
    mp4 = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom"
    calls = []

    class FakeDialer:
        def request(self, method, url, **kwargs):
            content = kwargs.get("content")
            payload = json.loads(content) if content else None
            calls.append((method, url, payload))
            if url.endswith("/images/generations"):
                data = base64.b64encode(png.getvalue()).decode()
                return httpx.Response(200, json={"data": [{"b64_json": data}]})
            if url.endswith("/audio/speech"):
                return httpx.Response(200, content=wav.getvalue())
            if url.endswith("/interactions"):
                return httpx.Response(200, json={"output_video": {"uri": "files/video-id"}})
            if url.endswith("/files/video-id"):
                return httpx.Response(200, json={"state": "ACTIVE"})
            assert url.endswith("/files/video-id:download?alt=media")
            return httpx.Response(200, content=mp4)

    openai = OpenAiHttpAdapter("test-key", dialer=FakeDialer())
    gemini = GeminiHttpAdapter("test-key", dialer=FakeDialer())
    assert synthetic_probe(openai, "gpt-image-local", "IMAGE").data == png.getvalue()
    assert synthetic_probe(openai, "tts-local", "TTS").data == wav.getvalue()
    assert synthetic_probe(gemini, "gemini-omni-1.2-flash", "VIDEO").data == mp4
    assert calls[0][2]["prompt"] == "Render a single blue circle on a white background."
    assert calls[1][2]["input"] == "سلام."
    assert calls[2][2]["response_format"]["duration"] == "3s"
    assert calls[-1][0] == "GET"

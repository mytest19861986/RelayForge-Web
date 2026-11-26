import re
from urllib.parse import urlencode, urlsplit

from relayforge.adapters.net_guard import SafeDialer
from relayforge.core.media_validation import MAX_BYTES, media_format

PEXELS = "https://api.pexels.com/v1"
LICENSE = "https://www.pexels.com/license/"


class PexelsStockAdapter:
    def __init__(self, key, *, dialer=None):
        self.key = key
        self.dialer = dialer or SafeDialer(extra_secrets=(key,))

    def _lookup(self, path):
        return self.dialer.request(
            "GET",
            PEXELS + path,
            headers={"Authorization": self.key},
            follow_redirects=False,
            max_response_bytes=1024 * 1024,
        ).json()

    @staticmethod
    def _record(photo):
        return {
            "provider": "PEXELS",
            "asset_id": str(photo["id"]),
            "photographer": photo["photographer"],
            "photographer_url": photo["photographer_url"],
            "source_url": photo["url"],
            "license_url": LICENSE,
            "preview_url": photo["src"]["medium"],
        }

    @staticmethod
    def _video_record(video):
        return {
            "provider": "PEXELS",
            "media_kind": "VIDEO",
            "asset_id": str(video["id"]),
            "photographer": video["user"]["name"],
            "photographer_url": video["user"]["url"],
            "source_url": video["url"],
            "license_url": LICENSE,
            "preview_url": video["image"],
        }

    def search(self, query, *, video=False):
        path, key, project = (
            ("/videos/search", "videos", self._video_record)
            if video
            else ("/search", "photos", self._record)
        )
        path += "?" + urlencode({"query": query, "per_page": 20})
        return [project(item) for item in self._lookup(path)[key]]

    def download(self, identifier, *, video=False):
        if not re.fullmatch(r"[0-9]{1,20}", identifier):
            raise ValueError("INVALID_STOCK_ID")
        if video:
            record = self._lookup("/videos/videos/" + identifier)
            files = [
                file
                for file in record["video_files"]
                if file.get("file_type") == "video/mp4"
                and type(file.get("width")) is int
                and 320 <= file["width"] <= 1920
            ]
            if not files:
                raise ValueError("INVALID_STOCK_FORMAT")
            url = min(files, key=lambda file: file["width"])["link"]
            attribution = self._video_record(record)
        else:
            record = self._lookup("/photos/" + identifier)
            url = record["src"]["original"]
            attribution = self._record(record)
        parsed = urlsplit(url)
        hosts = {"videos.pexels.com", "player.vimeo.com"} if video else {"images.pexels.com"}
        if parsed.scheme != "https" or parsed.netloc not in hosts:
            raise ValueError("INVALID_STOCK_URL")
        response = self.dialer.request(
            "GET", url, follow_redirects=True, max_response_bytes=MAX_BYTES
        )
        file_format, _ = media_format(response.content)
        if video != (file_format == "MP4"):
            raise ValueError("INVALID_STOCK_FORMAT")
        return response.content, attribution

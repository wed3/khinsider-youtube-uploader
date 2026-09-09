#!/usr/bin/env python3

"""
khinsider_to_youtube.py

Takes a KHInsider track-page URL such as:

  https://downloads.khinsider.com/game-soundtracks/album/<album>/<track>.mp3

and:
  1. reads the track + album metadata from KHInsider,
  2. downloads the MP3 exposed by the track page,
  3. finds the album cover (or prompts for an image URL),
  4. renders a still-image YouTube video,
  5. uploads it as: "Game Name (OST) - Track Name",
  6. sets the cover as the YouTube thumbnail.

Python deps inside the existing venv:

  python -m pip install requests pillow google-auth google-auth-oauthlib \
    google-auth-httplib2 google-api-python-client

System dep:

  sudo pacman -S ffmpeg

OAuth files default to client_secret.json and token.json beside this script.

Examples:

  python khinsider_to_youtube.py \
    "https://downloads.khinsider.com/game-soundtracks/album/the-dark-rites-of-arkham-original-soundtrack-2026/02.%2520Arkham%25201933.mp3" \
    --privacy public

Keep the rendered/downloaded files for testing:

  python khinsider_to_youtube.py "KHINSIDER_TRACK_URL" --keep-temp --no-upload

Override an incorrectly inferred game name or cover:

  python khinsider_to_youtube.py "KHINSIDER_TRACK_URL" \
    --game-name "The Dark Rites Of Arkham" \
    --cover-url "https://example.com/cover.jpg"
"""

import argparse
import html
from html.parser import HTMLParser
import pathlib
import re
import subprocess
import sys
from urllib.parse import parse_qs, unquote, urljoin, urlparse, urlunparse

import requests
from PIL import Image, ImageOps, UnidentifiedImageError

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/152.0 Safari/537.36"
)
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp")


class PageParser(HTMLParser):
    """Small dependency-free HTML extractor for KHInsider pages."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.headings: list[tuple[str, str]] = []
        self.links: list[dict] = []
        self.images: list[dict] = []
        self.metas: list[dict] = []
        self.text_chunks: list[str] = []

        self._in_title = False
        self._title_buffer: list[str] = []
        self._heading_tag: str | None = None
        self._heading_buffer: list[str] = []
        self._anchor_attrs: dict[str, str] | None = None
        self._anchor_buffer: list[str] = []

    @staticmethod
    def _attrs(attrs) -> dict[str, str]:
        return {str(key).lower(): (value or "") for key, value in attrs}

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        data = self._attrs(attrs)

        if tag == "title":
            self._in_title = True
            self._title_buffer = []
        elif tag in {"h1", "h2", "h3"}:
            self._heading_tag = tag
            self._heading_buffer = []
        elif tag == "a":
            self._anchor_attrs = data
            self._anchor_buffer = []
        elif tag == "img":
            item = dict(data)
            if self._anchor_attrs:
                item["parent_href"] = self._anchor_attrs.get("href", "")
            self.images.append(item)
        elif tag == "meta":
            self.metas.append(data)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()

        if tag == "title" and self._in_title:
            self.title_parts = self._title_buffer[:]
            self._in_title = False
            self._title_buffer = []
        elif tag == self._heading_tag:
            text = clean_spaces(" ".join(self._heading_buffer))
            if text:
                self.headings.append((tag, text))
            self._heading_tag = None
            self._heading_buffer = []
        elif tag == "a" and self._anchor_attrs is not None:
            item = dict(self._anchor_attrs)
            item["text"] = clean_spaces(" ".join(self._anchor_buffer))
            self.links.append(item)
            self._anchor_attrs = None
            self._anchor_buffer = []

    def handle_data(self, data: str) -> None:
        text = clean_spaces(data)
        if text:
            self.text_chunks.append(text)

        if self._in_title:
            self._title_buffer.append(data)
        if self._heading_tag is not None:
            self._heading_buffer.append(data)
        if self._anchor_attrs is not None:
            self._anchor_buffer.append(data)

    @property
    def title(self) -> str:
        return clean_spaces(" ".join(self.title_parts))


class KhinsiderError(RuntimeError):
    pass


def clean_spaces(text: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(text or "")).strip()


def slugify(text: str) -> str:
    text = re.sub(r"[^\w\s.-]", "", text, flags=re.UNICODE)
    text = re.sub(r"\s+", "_", text).strip("_")
    return text[:120] or "khinsider_upload"


def sanitize_youtube_title(title: str) -> str:
    title = title.replace("\n", " ").replace("\r", " ").replace("\t", " ")
    title = re.sub(r"[<>]", "", title)
    title = re.sub(r"\s+", " ", title).strip()

    if not title:
        raise RuntimeError("YouTube title became empty after sanitizing.")

    return title[:100]


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
    )
    return session


def fetch_html(session: requests.Session, url: str, referer: str | None = None) -> tuple[str, str]:
    headers = {"Referer": referer} if referer else None
    response = session.get(url, headers=headers, timeout=45, allow_redirects=True)
    response.raise_for_status()

    content_type = response.headers.get("Content-Type", "").lower()
    if "html" not in content_type and "xhtml" not in content_type:
        raise KhinsiderError(
            f"Expected an HTML track/album page but received {content_type or 'unknown content type'} from {response.url}"
        )

    return response.text, response.url


def parse_page(source: str) -> PageParser:
    parser = PageParser()
    parser.feed(source)
    parser.close()
    return parser


def label_value(chunks: list[str], label: str) -> str | None:
    """Find a value emitted near visible text such as 'Song name:' or 'Album name:'."""
    target = label.lower().rstrip(":")

    for index, chunk in enumerate(chunks):
        cleaned = clean_spaces(chunk)
        lower = cleaned.lower()

        if lower.startswith(target + ":"):
            value = cleaned.split(":", 1)[1].strip()
            if value:
                return value

            for following in chunks[index + 1 : index + 5]:
                following = clean_spaces(following)
                if following:
                    return following

        if lower == target:
            for following in chunks[index + 1 : index + 5]:
                following = clean_spaces(following)
                if following not in {":", "-"}:
                    return following.lstrip(":").strip()

    return None


def parent_album_url(track_url: str) -> str:
    parsed = urlparse(track_url)
    path = parsed.path.rstrip("/")
    if "/game-soundtracks/album/" not in path or "/" not in path.split("/album/", 1)[-1]:
        raise KhinsiderError(
            "That does not look like a KHInsider individual-track URL. "
            "Expected .../game-soundtracks/album/<album>/<track>.mp3"
        )

    album_path = path.rsplit("/", 1)[0]
    return urlunparse((parsed.scheme, parsed.netloc, album_path, "", "", ""))


def fallback_track_name_from_url(track_url: str) -> str:
    filename = pathlib.PurePosixPath(urlparse(track_url).path).name
    decoded = unquote(unquote(filename))
    decoded = re.sub(r"\.(?:mp3|flac|ogg|m4a|wav)$", "", decoded, flags=re.I)
    decoded = re.sub(r"^\s*\d+\s*[._-]\s*", "", decoded)
    return clean_spaces(decoded) or "Unknown Track"


def is_bad_track_name(value: str | None) -> bool:
    """Reject UI/download text that KHInsider sometimes places after an empty Song name field."""
    if not value:
        return True

    text = clean_spaces(value)
    lower = text.lower()

    if lower in {"get_app", "playlist_add", "file_download", "download"}:
        return True
    if "click here to download" in lower or "download as mp3" in lower or "download as flac" in lower:
        return True
    if re.fullmatch(r"\d+:\d{2}", text):
        return True
    if re.fullmatch(r"[\d.]+\s*(?:kb|mb|gb)", lower):
        return True

    return False


def parse_track_page(track_page: PageParser, track_url: str) -> tuple[str, str | None]:
    track_name = label_value(track_page.text_chunks, "Song name")
    album_name = label_value(track_page.text_chunks, "Album name")

    # Some older KHInsider track pages leave "Song name:" blank.  The generic
    # label reader can then encounter the download button text ("get_app...").
    if is_bad_track_name(track_name):
        track_name = None

    if not track_name and track_page.title:
        match = re.match(r"(.+?)\s+MP3\s+-\s+", track_page.title, flags=re.I)
        if match:
            candidate = clean_spaces(match.group(1))
            if not is_bad_track_name(candidate):
                track_name = candidate

    if not track_name:
        candidate = fallback_track_name_from_url(track_url)
        if not is_bad_track_name(candidate):
            track_name = candidate

    return track_name or "Unknown Track", album_name


def track_name_from_album_page(
    album_page: PageParser,
    album_page_url: str,
    track_page_url: str,
) -> str | None:
    """Find the song title by matching the track-page URL on the album track list."""
    target = urlparse(track_page_url)
    target_path = unquote(unquote(target.path)).rstrip("/")

    for link in album_page.links:
        href = (link.get("href") or "").strip()
        text = clean_spaces(link.get("text") or "")
        if not href or is_bad_track_name(text):
            continue

        absolute = urljoin(album_page_url, href)
        parsed = urlparse(absolute)
        candidate_path = unquote(unquote(parsed.path)).rstrip("/")

        if parsed.netloc.lower() == target.netloc.lower() and candidate_path == target_path:
            return text

    return None


def find_real_audio_url(track_page: PageParser, track_page_url: str) -> str:
    scored: list[tuple[int, str]] = []

    for link in track_page.links:
        href = link.get("href", "").strip()
        text = link.get("text", "").lower()
        if not href:
            continue

        absolute = urljoin(track_page_url, href)
        parsed = urlparse(absolute)
        path_lower = unquote(parsed.path).lower()
        score = 0

        if "download as mp3" in text:
            score += 100
        if "click here to download" in text:
            score += 80
        if path_lower.endswith(".mp3"):
            score += 40
        if parsed.netloc and parsed.netloc.lower() != urlparse(track_page_url).netloc.lower():
            score += 10

        if score:
            scored.append((score, absolute))

    if not scored:
        raise KhinsiderError("Could not find the actual MP3 download link on the KHInsider track page.")

    scored.sort(key=lambda item: item[0], reverse=True)
    return scored[0][1]


def album_heading(album_page: PageParser, fallback: str | None = None) -> str:
    for tag, text in album_page.headings:
        if tag == "h2" and text:
            return strip_year_suffix(text)

    if fallback:
        return strip_year_suffix(fallback)

    if album_page.title:
        title = re.sub(r"\s*\(\d{4}\)\s*MP3.*$", "", album_page.title, flags=re.I)
        title = re.sub(r"\s*MP3\s*-.*$", "", title, flags=re.I)
        if title:
            return strip_year_suffix(title)

    raise KhinsiderError("Could not determine the album name from KHInsider.")


def strip_year_suffix(text: str) -> str:
    return re.sub(r"\s*\(\d{4}\)\s*$", "", clean_spaces(text)).strip()


def infer_game_name(album_name: str) -> str:
    """Best-effort conversion from KHInsider album title to the underlying game name."""
    name = strip_year_suffix(album_name)

    patterns = [
        r"\s*[-–—:]\s*(?:the\s+)?original\s+(?:game\s+)?soundtrack\s*$",
        r"\s*[-–—:]\s*original\s+soundtrack\s*$",
        r"\s*[-–—:]\s*(?:official\s+)?soundtrack\s*$",
        r"\s*[-–—:]\s*ost\s*$",
        r"\s*\((?:original\s+)?soundtrack\)\s*$",
        r"\s*\(ost\)\s*$",
        r"\s+(?:original\s+)?soundtrack\s*$",
        r"\s+ost\s*$",
    ]

    previous = None
    while previous != name:
        previous = name
        for pattern in patterns:
            name = re.sub(pattern, "", name, flags=re.I).strip(" -–—:")

    return name or strip_year_suffix(album_name)


def normalize_image_url(value: str, base_url: str) -> str | None:
    value = html.unescape((value or "").strip())
    if not value or value.startswith(("data:", "javascript:")):
        return None

    # srcset can contain "URL 1x, URL 2x". Prefer the last/largest entry.
    if "," in value and any(token in value for token in (" 1x", " 2x", " 300w", " 600w", " 1000w")):
        parts = [part.strip().split()[0] for part in value.split(",") if part.strip()]
        if parts:
            value = parts[-1]

    return urljoin(base_url, value)


def looks_like_image_url(url: str) -> bool:
    path = urlparse(url).path.lower()
    return path.endswith(IMAGE_EXTENSIONS) or "image" in path or "cover" in path


def cover_candidates(album_page: PageParser, raw_html: str, album_url: str, album_name: str) -> list[str]:
    """Return likely album-art URLs, best candidates first."""
    candidates: dict[str, int] = {}
    album_words = {
        word.lower()
        for word in re.findall(r"[A-Za-z0-9]+", album_name)
        if len(word) >= 4 and word.lower() not in {"original", "soundtrack", "album"}
    }

    def add(value: str | None, score: int, context: str = "") -> None:
        if not value:
            return
        url = normalize_image_url(value, album_url)
        if not url or not url.startswith(("http://", "https://")):
            return

        lower_url = url.lower()
        lower_context = context.lower()

        if any(bad in lower_url for bad in ("logo", "favicon", "sprite", "icon", "avatar", "emoji", "banner", "ads")):
            score -= 120
        if any(bad in lower_context for bad in ("logo", "avatar", "profile", "comment", "icon")):
            score -= 100

        if "images.khinsider.com" in lower_url:
            score += 35
        if any(good in lower_url for good in ("cover", "album", "front", "folder")):
            score += 35
        if any(good in lower_context for good in ("cover", "album", "front", "artwork")):
            score += 45

        matched_words = sum(1 for word in album_words if word in lower_context or word in lower_url)
        score += min(matched_words * 8, 40)

        candidates[url] = max(score, candidates.get(url, -9999))

    for meta in album_page.metas:
        key = (meta.get("property") or meta.get("name") or "").lower()
        content = meta.get("content", "")
        if key in {"og:image", "og:image:url", "twitter:image", "twitter:image:src"}:
            add(content, 120, key)

    for image in album_page.images:
        context = " ".join(
            image.get(key, "")
            for key in ("id", "class", "alt", "title", "src", "data-src", "data-original")
        )

        for key, bonus in (
            ("data-original", 95),
            ("data-src", 90),
            ("src", 85),
            ("srcset", 80),
        ):
            add(image.get(key), bonus, context)

        parent_href = image.get("parent_href", "")
        if parent_href and looks_like_image_url(parent_href):
            add(parent_href, 105, context + " linked full image")

    # Fallback for image URLs embedded in inline script/style/JSON that the parser did not expose.
    for match in re.finditer(
        r"https?://[^\s\"'<>\\]+?\.(?:jpe?g|png|webp)(?:\?[^\s\"'<>\\]*)?",
        raw_html,
        flags=re.I,
    ):
        add(match.group(0), 35, "raw html image")

    ranked = sorted(candidates.items(), key=lambda item: item[1], reverse=True)
    return [url for url, score in ranked if score > 0]


def download_binary(
    session: requests.Session,
    url: str,
    path: pathlib.Path,
    referer: str | None = None,
) -> str:
    headers = {"Referer": referer} if referer else None
    with session.get(url, headers=headers, stream=True, timeout=90, allow_redirects=True) as response:
        response.raise_for_status()
        with open(path, "wb") as file:
            for chunk in response.iter_content(chunk_size=1024 * 256):
                if chunk:
                    file.write(chunk)
        return response.url


def valid_cover(path: pathlib.Path) -> tuple[bool, str]:
    try:
        with Image.open(path) as image:
            width, height = image.size
            image.verify()
    except (UnidentifiedImageError, OSError) as error:
        return False, f"not a readable image ({error})"

    if width < 180 or height < 180:
        return False, f"image is only {width}x{height}"

    if width / max(height, 1) > 3.0 or height / max(width, 1) > 3.0:
        return False, f"image aspect ratio is suspicious ({width}x{height})"

    return True, f"{width}x{height}"


def try_automatic_cover(
    session: requests.Session,
    candidates: list[str],
    output_path: pathlib.Path,
    album_url: str,
) -> tuple[str | None, str | None]:
    for index, url in enumerate(candidates[:20], start=1):
        try:
            print(f"Trying cover candidate {index}: {url}")
            final_url = download_binary(session, url, output_path, referer=album_url)
            valid, detail = valid_cover(output_path)
            if valid:
                print(f"Using KHInsider cover art ({detail}).")
                return final_url, None
            print(f"Skipping candidate: {detail}")
        except requests.RequestException as error:
            print(f"Skipping candidate: {error}")
        finally:
            if output_path.exists():
                valid, _ = valid_cover(output_path)
                if not valid:
                    output_path.unlink(missing_ok=True)

    return None, "KHInsider did not expose a usable cover image that could be detected automatically."


def prompt_for_cover(
    session: requests.Session,
    output_path: pathlib.Path,
    referer: str | None = None,
) -> str:
    print()
    print("No usable KHInsider cover art was found.")

    while True:
        url = input("Paste a cover-art image URL: ").strip()
        if not url:
            print("A URL is required.")
            continue

        try:
            final_url = download_binary(session, url, output_path, referer=referer)
            valid, detail = valid_cover(output_path)
            if valid:
                print(f"Using supplied cover art ({detail}).")
                return final_url
            print(f"That URL did not produce a usable cover image: {detail}")
        except requests.RequestException as error:
            print(f"Could not download that image: {error}")
        finally:
            if output_path.exists():
                valid, _ = valid_cover(output_path)
                if not valid:
                    output_path.unlink(missing_ok=True)


def make_cover_image(
    cover_path: pathlib.Path,
    output_path: pathlib.Path,
    size: tuple[int, int],
    max_bytes: int | None = None,
) -> None:
    """Preserve the full cover art centered on a black 16:9 frame."""
    source = Image.open(cover_path).convert("RGB")
    background = Image.new("RGB", size, (0, 0, 0))

    cover = ImageOps.contain(
        source,
        size,
        method=Image.Resampling.LANCZOS,
    )

    x = (size[0] - cover.width) // 2
    y = (size[1] - cover.height) // 2
    background.paste(cover, (x, y))

    quality = 95

    while True:
        background.save(output_path, "JPEG", quality=quality, optimize=True)

        if max_bytes is None or output_path.stat().st_size <= max_bytes:
            return

        quality -= 5
        if quality < 70:
            return


def render_video(audio_path: pathlib.Path, image_path: pathlib.Path, output_path: pathlib.Path) -> None:
    command = [
        "ffmpeg",
        "-y",
        "-loop",
        "1",
        "-framerate",
        "30",
        "-i",
        str(image_path),
        "-i",
        str(audio_path),
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-c:v",
        "libx264",
        "-preset",
        "slow",
        "-crf",
        "18",
        "-tune",
        "stillimage",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "384k",
        "-shortest",
        "-movflags",
        "+faststart",
        str(output_path),
    ]

    subprocess.run(command, check=True)


def get_youtube_client_manual_oauth(
    client_secret_path: pathlib.Path,
    token_path: pathlib.Path,
):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    credentials = None

    if token_path.exists():
        credentials = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if credentials and credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())

    if not credentials or not credentials.valid:
        flow = InstalledAppFlow.from_client_secrets_file(
            str(client_secret_path),
            SCOPES,
            redirect_uri="http://127.0.0.1:8080/",
        )

        auth_url, expected_state = flow.authorization_url(
            access_type="offline",
            prompt="consent",
        )

        print()
        print("Open Google authorization URL in browser:")
        print()
        print(auth_url)
        print()
        print("After approval, Google should redirect to a URL beginning with:")
        print()
        print("  http://127.0.0.1:8080/?")
        print()
        print("The page may look broken or refuse to load. That is fine.")
        print("Copy the full final URL from the browser address bar and paste it below.")
        print()

        redirect_response = input("Paste final redirected URL here: ").strip()

        if not redirect_response.startswith("http://127.0.0.1:8080/"):
            raise RuntimeError(
                "Expected final redirected URL beginning with "
                "http://127.0.0.1:8080/. Something else was pasted."
            )

        parsed_redirect = urlparse(redirect_response)
        query = parse_qs(parsed_redirect.query)

        if "error" in query:
            raise RuntimeError(f"Google OAuth error: {query['error'][0]}")

        state_values = query.get("state")
        if not state_values:
            raise RuntimeError("Could not find state=... in redirected URL.")

        if state_values[0] != expected_state:
            raise RuntimeError("OAuth state mismatch. Rerun and try again.")

        code_values = query.get("code")
        if not code_values:
            raise RuntimeError("Could not find code=... in redirected URL.")

        authorization_code = code_values[0]
        flow.fetch_token(code=authorization_code)
        credentials = flow.credentials

    token_path.write_text(credentials.to_json())
    return build("youtube", "v3", credentials=credentials)


def upload_video(
    youtube,
    video_path: pathlib.Path,
    title: str,
    description: str,
    privacy: str,
) -> str:
    from googleapiclient.http import MediaFileUpload

    body = {
        "snippet": {
            "title": title,
            "description": description,
            "categoryId": "10",
        },
        "status": {
            "privacyStatus": privacy,
            "selfDeclaredMadeForKids": False,
        },
    }

    media = MediaFileUpload(
        str(video_path),
        chunksize=1024 * 1024 * 8,
        resumable=True,
    )

    request = youtube.videos().insert(
        part="snippet,status",
        body=body,
        media_body=media,
    )

    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            print(f"Upload progress: {int(status.progress() * 100)}%")

    return response["id"]


def set_thumbnail(youtube, video_id: str, thumbnail_path: pathlib.Path) -> None:
    from googleapiclient.http import MediaFileUpload

    media = MediaFileUpload(str(thumbnail_path))
    youtube.thumbnails().set(
        videoId=video_id,
        media_body=media,
    ).execute()


def build_description(
    youtube_title: str,
    track_page_url: str,
    album_url: str,
) -> str:
    return (
        f"{youtube_title}\n\n"
        f"Source track page: {track_page_url}\n"
        f"Album page: {album_url}\n"
    )


def cleanup_temp_files(paths: list[pathlib.Path]) -> None:
    print("Cleaning up temporary files.")

    for path in paths:
        try:
            if path.exists() and path.is_file():
                path.unlink()
                print(f"Deleted: {path}")
        except Exception as error:
            print(f"Warning: could not delete {path}: {error}", file=sys.stderr)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Turn one KHInsider track into a cover-art YouTube video and upload it."
    )

    parser.add_argument(
        "khinsider_url",
        help="KHInsider individual-track URL, normally ending in .mp3 even though it opens an HTML track page.",
    )

    parser.add_argument(
        "--game-name",
        help="Override the game name inferred from the KHInsider album title.",
    )

    parser.add_argument(
        "--coverart",
        "--cover-art",
        "--cover-url",
        dest="cover_url",
        help="Override KHInsider cover detection with a direct image URL.",
    )

    parser.add_argument(
        "--privacy",
        default="private",
        choices=["private", "unlisted", "public"],
    )

    parser.add_argument(
        "--client-secret",
        default=str(SCRIPT_DIR / "client_secret.json"),
        help="Google OAuth client secret JSON. Defaults beside this script.",
    )

    parser.add_argument(
        "--token",
        default=str(SCRIPT_DIR / "token.json"),
        help="Saved OAuth token. Defaults beside this script.",
    )

    parser.add_argument(
        "--outdir",
        default=str(SCRIPT_DIR / "out"),
        help="Folder for downloaded audio, cover art, rendered video, and thumbnail.",
    )

    parser.add_argument(
        "--keep-temp",
        action="store_true",
        help="Keep generated files after a successful upload.",
    )

    parser.add_argument(
        "--no-upload",
        action="store_true",
        help="Download metadata/audio/cover and render the MP4, but do not authenticate or upload.",
    )

    return parser.parse_args()


def main() -> None:
    args = parse_args()
    session = make_session()

    track_page_url = args.khinsider_url.strip()
    if not track_page_url.startswith(("http://", "https://")):
        raise KhinsiderError("Please provide a full http:// or https:// KHInsider URL.")

    print("Fetching KHInsider track page.")
    track_html, resolved_track_url = fetch_html(session, track_page_url)
    track_page = parse_page(track_html)

    track_name, track_album_name = parse_track_page(track_page, resolved_track_url)
    audio_url = find_real_audio_url(track_page, resolved_track_url)
    album_url = parent_album_url(resolved_track_url)

    print("Fetching KHInsider album page.")
    album_html, resolved_album_url = fetch_html(session, album_url, referer=resolved_track_url)
    album_page = parse_page(album_html)
    album_name = album_heading(album_page, fallback=track_album_name)

    # Prefer the album track list when it contains a title for this exact URL.
    # This fixes older pages whose individual track page has a blank Song name.
    album_track_name = track_name_from_album_page(
        album_page, resolved_album_url, resolved_track_url
    )
    if album_track_name:
        track_name = album_track_name

    game_name = clean_spaces(args.game_name) if args.game_name else infer_game_name(album_name)
    youtube_title = sanitize_youtube_title(f"{game_name} (OST) - {track_name}")
    stem = slugify(youtube_title)

    outdir = pathlib.Path(args.outdir).expanduser().resolve()
    outdir.mkdir(parents=True, exist_ok=True)

    raw_cover_path = outdir / f"{stem}_cover_raw"
    audio_path = outdir / f"{stem}.mp3"
    video_frame_path = outdir / f"{stem}_video_frame.jpg"
    youtube_thumbnail_path = outdir / f"{stem}_youtube_thumbnail.jpg"
    video_path = outdir / f"{stem}.mp4"

    print()
    print(f"Album: {album_name}")
    print(f"Game: {game_name}")
    print(f"Track: {track_name}")
    print(f"YouTube title: {youtube_title}")
    print(f"Album URL: {resolved_album_url}")
    print(f"Audio URL: {audio_url}")
    print()

    if args.cover_url:
        print("Downloading cover art from --cover-url.")
        cover_url = download_binary(session, args.cover_url, raw_cover_path, referer=resolved_album_url)
        valid, detail = valid_cover(raw_cover_path)
        if not valid:
            raw_cover_path.unlink(missing_ok=True)
            raise KhinsiderError(f"--cover-url did not produce a usable image: {detail}")
        print(f"Using supplied cover art ({detail}).")
    else:
        print("Looking for KHInsider album cover art.")
        candidates = cover_candidates(album_page, album_html, resolved_album_url, album_name)
        cover_url, cover_error = try_automatic_cover(
            session,
            candidates,
            raw_cover_path,
            resolved_album_url,
        )
        if not cover_url:
            if cover_error:
                print(cover_error)
            cover_url = prompt_for_cover(session, raw_cover_path, referer=resolved_album_url)

    print(f"Cover URL: {cover_url}")

    print("Downloading MP3.")
    download_binary(session, audio_url, audio_path, referer=resolved_track_url)
    if not audio_path.exists() or audio_path.stat().st_size < 1024:
        raise KhinsiderError("Downloaded audio file is missing or suspiciously small.")

    print("Creating 16:9 video frame and YouTube thumbnail.")
    make_cover_image(raw_cover_path, video_frame_path, size=(1920, 1080))
    make_cover_image(
        raw_cover_path,
        youtube_thumbnail_path,
        size=(1280, 720),
        max_bytes=2_000_000,
    )

    print("Rendering video.")
    render_video(audio_path, video_frame_path, video_path)
    print(f"Rendered: {video_path}")

    temp_files = [
        raw_cover_path,
        audio_path,
        video_frame_path,
        youtube_thumbnail_path,
        video_path,
    ]

    if args.no_upload:
        print("--no-upload used; skipping YouTube authentication/upload.")
        print(f"Video ready at: {video_path}")
        return

    client_secret_path = pathlib.Path(args.client_secret).expanduser().resolve()
    token_path = pathlib.Path(args.token).expanduser().resolve()

    if not client_secret_path.exists():
        raise FileNotFoundError(f"Missing OAuth client secret file: {client_secret_path}")

    print("Authenticating YouTube account.")
    youtube = get_youtube_client_manual_oauth(
        client_secret_path=client_secret_path,
        token_path=token_path,
    )

    description = build_description(
        youtube_title=youtube_title,
        track_page_url=resolved_track_url,
        album_url=resolved_album_url,
    )

    print("Uploading to YouTube.")
    video_id = upload_video(
        youtube=youtube,
        video_path=video_path,
        title=youtube_title,
        description=description,
        privacy=args.privacy,
    )

    print(f"YouTube video ID: {video_id}")

    try:
        set_thumbnail(youtube, video_id, youtube_thumbnail_path)
        print("Thumbnail set.")
    except Exception as error:
        print(f"Warning: video uploaded, but thumbnail failed: {error}", file=sys.stderr)

    print(f"https://www.youtube.com/watch?v={video_id}")

    if not args.keep_temp:
        cleanup_temp_files(temp_files)
    else:
        print("Keeping temporary files because --keep-temp was used.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        raise SystemExit(130)
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1)

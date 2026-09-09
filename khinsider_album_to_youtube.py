#!/usr/bin/env python3

"""
khinsider_album_to_youtube.py

Build one YouTube video from an entire KHInsider album.

Expected setup:
  - this file lives beside khinsider_to_youtube.py
  - both use the same Python environment
  - client_secret.json and token.json live beside the scripts
  - ffmpeg + ffprobe are installed

Accepted input:
  1. Preferred: a public album page
     https://downloads.khinsider.com/game-soundtracks/album/<album-slug>

  2. A KHInsider mass-download action such as
     https://downloads.khinsider.com/cp/add_album/46444

     That route is login-gated. If KHInsider does not redirect it to the
     public album page, this script prompts once for the public album URL.

The script:
  - reads the album track list in page order,
  - downloads every MP3 through its normal KHInsider track page,
  - normalizes each track to matching AAC parameters,
  - concatenates them in order,
  - generates YouTube chapter timestamps from the normalized track lengths,
  - uses the album cover for a still-image 1920x1080 video + thumbnail,
  - uploads with the same Google OAuth credentials as khinsider_to_youtube.py.

Examples:

  python khinsider_album_to_youtube.py \
    "https://downloads.khinsider.com/game-soundtracks/album/the-dark-rites-of-arkham-original-soundtrack-2026" \
    --privacy public

  python khinsider_album_to_youtube.py \
    "https://downloads.khinsider.com/cp/add_album/46444" \
    --privacy public

Test everything except YouTube upload:

  python khinsider_album_to_youtube.py "KHINSIDER_ALBUM_URL" \
    --no-upload --keep-temp

Overrides:

  --game-name "Exact Game Name"
  --title "Exact YouTube Title"
  --coverart "https://example.com/cover.jpg"
  --album-url "https://downloads.khinsider.com/game-soundtracks/album/..."
"""

import argparse
import pathlib
import re
import shutil
import subprocess
import sys
import time
from urllib.parse import unquote, urljoin, urlparse, urlunparse

try:
    import khinsider_to_youtube as khi
except ImportError as error:
    raise SystemExit(
        "Could not import khinsider_to_youtube.py. Put this script in the same "
        "directory as khinsider_to_youtube.py."
    ) from error


SCRIPT_DIR = pathlib.Path(__file__).resolve().parent


def is_public_album_url(url: str) -> bool:
    parsed = urlparse(url)
    marker = "/game-soundtracks/album/"
    if marker not in parsed.path:
        return False
    remainder = parsed.path.split(marker, 1)[1].strip("/")
    return bool(remainder) and "/" not in remainder


def is_track_url(url: str) -> bool:
    parsed = urlparse(url)
    marker = "/game-soundtracks/album/"
    if marker not in parsed.path:
        return False
    remainder = parsed.path.split(marker, 1)[1].strip("/")
    return remainder.count("/") >= 1


def normalize_page_url(url: str) -> str:
    parsed = urlparse(url.strip())
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", "", ""))


def resolve_album_input(session, supplied_url: str, album_override: str | None) -> str:
    if album_override:
        candidate = normalize_page_url(album_override)
        if not is_public_album_url(candidate):
            raise khi.KhinsiderError("--album-url must be a public KHInsider /game-soundtracks/album/<slug> URL.")
        return candidate

    supplied_url = supplied_url.strip()
    if not supplied_url.startswith(("http://", "https://")):
        raise khi.KhinsiderError("Please provide a full http:// or https:// KHInsider URL.")

    candidate = normalize_page_url(supplied_url)

    if is_public_album_url(candidate):
        return candidate

    if is_track_url(candidate):
        return khi.parent_album_url(candidate)

    parsed = urlparse(candidate)
    if re.fullmatch(r"/cp/add_album/\d+", parsed.path):
        # In a logged-in session KHInsider may redirect this somewhere useful.
        # With a normal unauthenticated requests.Session it currently returns a
        # login page and does not expose the public album slug.
        try:
            _, resolved = khi.fetch_html(session, candidate)
            resolved = normalize_page_url(resolved)
            if is_public_album_url(resolved):
                print(f"Resolved mass-download URL to album page: {resolved}")
                return resolved
        except Exception:
            pass

        print()
        print("That /cp/add_album/... URL is KHInsider's login-gated mass-download action.")
        print("It does not expose the public album slug to an unauthenticated script.")
        print()
        while True:
            pasted = input("Paste the album page URL from your browser address bar: ").strip()
            pasted = normalize_page_url(pasted)
            if is_public_album_url(pasted):
                return pasted
            print("Expected https://downloads.khinsider.com/game-soundtracks/album/<album-slug>")

    raise khi.KhinsiderError(
        "Expected a KHInsider public album URL, individual-track URL, or /cp/add_album/<id> URL."
    )


def extract_album_tracks(album_page, album_url: str) -> list[dict]:
    """Extract unique track-page URLs in the order shown by KHInsider."""
    parsed_album = urlparse(album_url)
    album_path = parsed_album.path.rstrip("/")

    order: list[str] = []
    grouped: dict[str, dict] = {}

    for link in album_page.links:
        href = (link.get("href") or "").strip()
        if not href:
            continue

        absolute = normalize_page_url(urljoin(album_url, href))
        parsed = urlparse(absolute)

        if parsed.netloc.lower() != parsed_album.netloc.lower():
            continue

        decoded_path = unquote(unquote(parsed.path))
        decoded_album_path = unquote(unquote(album_path))

        if not decoded_path.startswith(decoded_album_path + "/"):
            continue

        remainder = decoded_path[len(decoded_album_path) + 1 :]
        if not remainder or "/" in remainder:
            continue

        # KHInsider track-page URLs normally end in an audio-looking extension,
        # even though requesting them returns HTML.
        if not re.search(r"\.(?:mp3|flac|ogg|m4a|wav)$", remainder, flags=re.I):
            continue

        key = absolute
        text = khi.clean_spaces(link.get("text") or "")

        if key not in grouped:
            grouped[key] = {"url": absolute, "name": None}
            order.append(key)

        if not grouped[key]["name"] and not khi.is_bad_track_name(text):
            grouped[key]["name"] = text

    tracks = []
    for index, key in enumerate(order, start=1):
        item = grouped[key]
        name = item["name"]
        if not name or khi.is_bad_track_name(name):
            name = khi.fallback_track_name_from_url(item["url"])
        # A bare numeric filename such as 03.mp3 is not a useful title.
        if not name or khi.is_bad_track_name(name) or re.fullmatch(r"\d+", name):
            name = f"Track {index}"

        tracks.append(
            {
                "number": index,
                "name": name,
                "track_page_url": item["url"],
            }
        )

    if not tracks:
        raise khi.KhinsiderError("Could not find any tracks on the KHInsider album page.")

    return tracks


def safe_track_file(index: int, suffix: str) -> str:
    return f"track_{index:04d}{suffix}"


def ffprobe_audio_duration(path: pathlib.Path) -> float:
    """Return real audio duration by summing packet durations.

    Do not use ``format=duration`` for raw ADTS AAC. Raw AAC has no container
    timeline, so ffprobe may estimate its duration from bitrate and can be wildly
    wrong for some files. Packet durations come from the actual audio frames.
    """
    command = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "a:0",
        "-show_entries",
        "packet=duration_time",
        "-of",
        "csv=p=0",
        str(path),
    ]
    result = subprocess.run(command, check=True, capture_output=True, text=True)

    total = 0.0
    packet_count = 0

    for line in result.stdout.splitlines():
        value = line.split(",", 1)[0].strip()
        if not value or value.upper() == "N/A":
            continue
        try:
            duration = float(value)
        except ValueError:
            continue
        if duration > 0:
            total += duration
            packet_count += 1

    if packet_count == 0 or total <= 0:
        raise RuntimeError(f"Could not determine audio packet duration for {path}.")

    return total


def normalize_audio(source: pathlib.Path, output: pathlib.Path) -> None:
    command = [
        "ffmpeg",
        "-y",
        "-v",
        "warning",
        "-i",
        str(source),
        "-vn",
        "-c:a",
        "aac",
        "-b:a",
        "384k",
        "-ar",
        "48000",
        "-ac",
        "2",
        "-f",
        "adts",
        str(output),
    ]
    subprocess.run(command, check=True)


def concat_audio(paths: list[pathlib.Path], output: pathlib.Path) -> None:
    """Concatenate normalized AAC/ADTS files without another lossy encode."""
    with open(output, "wb") as destination:
        for path in paths:
            with open(path, "rb") as source:
                shutil.copyfileobj(source, destination, length=1024 * 1024)


def render_album_video(audio_path: pathlib.Path, image_path: pathlib.Path, output_path: pathlib.Path) -> None:
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
        "copy",
        "-shortest",
        "-movflags",
        "+faststart",
        str(output_path),
    ]
    subprocess.run(command, check=True)


def format_timestamp(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def make_chapters(tracks: list[dict]) -> tuple[list[str], float]:
    lines: list[str] = []
    current = 0.0

    for item in tracks:
        lines.append(f"{format_timestamp(current)} {item['name']}")
        current += item["duration"]

    return lines, current


def build_description(youtube_title: str, album_url: str, chapter_lines: list[str]) -> str:
    prefix = f"{youtube_title}\n\nChapters:\n"
    suffix = f"\n\nSource album: {album_url}\n"

    description = prefix + "\n".join(chapter_lines) + suffix
    if len(description) <= 5000:
        return description

    # Keep every chapter marker, but progressively shorten very long names so
    # giant albums have a better chance of fitting YouTube's 5,000-char limit.
    for max_name in (70, 55, 40, 30, 20):
        compact = []
        for line in chapter_lines:
            match = re.match(r"(\S+)\s+(.*)", line)
            if not match:
                compact.append(line)
                continue
            stamp, name = match.groups()
            if len(name) > max_name:
                name = name[: max_name - 1].rstrip() + "…"
            compact.append(f"{stamp} {name}")

        description = prefix + "\n".join(compact) + suffix
        if len(description) <= 5000:
            print(f"Warning: chapter names were shortened to fit YouTube's description limit ({max_name} chars max).")
            return description

    raise RuntimeError(
        "The chapter list cannot fit inside YouTube's 5,000-character description limit, even after shortening names."
    )


def remove_temp_tree(paths: list[pathlib.Path], directories: list[pathlib.Path]) -> None:
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except Exception as error:
            print(f"Warning: could not delete {path}: {error}", file=sys.stderr)

    for directory in directories:
        try:
            if directory.exists():
                shutil.rmtree(directory)
        except Exception as error:
            print(f"Warning: could not delete {directory}: {error}", file=sys.stderr)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Turn an entire KHInsider album into one chaptered YouTube soundtrack video."
    )
    parser.add_argument("khinsider_url")
    parser.add_argument(
        "--album-url",
        help="Public album page override. Useful when the first argument is /cp/add_album/<id>.",
    )
    parser.add_argument("--game-name", help="Override the inferred game name.")
    parser.add_argument("--title", help="Override the complete YouTube title.")
    parser.add_argument(
        "--coverart",
        "--cover-art",
        "--cover-url",
        dest="cover_url",
        help="Use this image instead of KHInsider cover detection.",
    )
    parser.add_argument(
        "--privacy",
        default="private",
        choices=["private", "unlisted", "public"],
    )
    parser.add_argument(
        "--client-secret",
        default=str(SCRIPT_DIR / "client_secret.json"),
    )
    parser.add_argument(
        "--token",
        default=str(SCRIPT_DIR / "token.json"),
    )
    parser.add_argument(
        "--outdir",
        default=str(SCRIPT_DIR / "out"),
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.35,
        help="Pause between KHInsider track requests/downloads (default: 0.35 seconds).",
    )
    parser.add_argument("--keep-temp", action="store_true")
    parser.add_argument("--no-upload", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    session = khi.make_session()

    album_url = resolve_album_input(session, args.khinsider_url, args.album_url)

    print(f"Fetching KHInsider album page: {album_url}")
    album_html, resolved_album_url = khi.fetch_html(session, album_url)
    album_page = khi.parse_page(album_html)
    album_name = khi.album_heading(album_page)
    game_name = khi.clean_spaces(args.game_name) if args.game_name else khi.infer_game_name(album_name)

    if args.title:
        youtube_title = khi.sanitize_youtube_title(args.title)
    else:
        youtube_title = khi.sanitize_youtube_title(f"{game_name} (OST) - Full Soundtrack")

    tracks = extract_album_tracks(album_page, resolved_album_url)
    stem = khi.slugify(youtube_title)

    outdir = pathlib.Path(args.outdir).expanduser().resolve()
    outdir.mkdir(parents=True, exist_ok=True)
    track_dir = outdir / f"{stem}_tracks"
    track_dir.mkdir(parents=True, exist_ok=True)

    raw_cover_path = outdir / f"{stem}_cover_raw"
    video_frame_path = outdir / f"{stem}_video_frame.jpg"
    thumbnail_path = outdir / f"{stem}_youtube_thumbnail.jpg"
    combined_audio = outdir / f"{stem}_album_audio.aac"
    video_path = outdir / f"{stem}.mp4"

    print()
    print(f"Album: {album_name}")
    print(f"Game: {game_name}")
    print(f"Tracks: {len(tracks)}")
    print(f"YouTube title: {youtube_title}")
    print()

    if args.cover_url:
        print("Downloading cover art from --coverart.")
        cover_url = khi.download_binary(
            session, args.cover_url, raw_cover_path, referer=resolved_album_url
        )
        valid, detail = khi.valid_cover(raw_cover_path)
        if not valid:
            raw_cover_path.unlink(missing_ok=True)
            raise khi.KhinsiderError(f"--coverart did not produce a usable image: {detail}")
        print(f"Using supplied cover art ({detail}).")
    else:
        print("Looking for KHInsider album cover art.")
        candidates = khi.cover_candidates(album_page, album_html, resolved_album_url, album_name)
        cover_url, cover_error = khi.try_automatic_cover(
            session, candidates, raw_cover_path, resolved_album_url
        )
        if not cover_url:
            if cover_error:
                print(cover_error)
            cover_url = khi.prompt_for_cover(session, raw_cover_path, referer=resolved_album_url)

    print(f"Cover URL: {cover_url}")

    khi.make_cover_image(raw_cover_path, video_frame_path, size=(1920, 1080))
    khi.make_cover_image(
        raw_cover_path,
        thumbnail_path,
        size=(1280, 720),
        max_bytes=2_000_000,
    )

    normalized_paths: list[pathlib.Path] = []

    print()
    print("Downloading and normalizing album tracks.")

    for item in tracks:
        index = item["number"]
        name = item["name"]
        track_page_url = item["track_page_url"]
        print(f"[{index}/{len(tracks)}] {name}")

        track_html, resolved_track_url = khi.fetch_html(
            session, track_page_url, referer=resolved_album_url
        )
        track_page = khi.parse_page(track_html)
        audio_url = khi.find_real_audio_url(track_page, resolved_track_url)

        mp3_path = track_dir / safe_track_file(index, ".mp3")
        normalized_path = track_dir / safe_track_file(index, ".aac")

        khi.download_binary(session, audio_url, mp3_path, referer=resolved_track_url)
        if not mp3_path.exists() or mp3_path.stat().st_size < 1024:
            raise khi.KhinsiderError(f"Downloaded track is missing or suspiciously small: {name}")

        source_duration = ffprobe_audio_duration(mp3_path)
        normalize_audio(mp3_path, normalized_path)
        normalized_duration = ffprobe_audio_duration(normalized_path)

        # Transcoding should not materially change the track length. Abort rather
        # than uploading bad chapters if either source or normalized timing is odd.
        duration_delta = abs(normalized_duration - source_duration)
        duration_tolerance = max(2.0, source_duration * 0.01)
        if duration_delta > duration_tolerance:
            raise RuntimeError(
                f"Duration sanity check failed for {name!r}: source="
                f"{format_timestamp(source_duration)}, normalized="
                f"{format_timestamp(normalized_duration)}."
            )

        item["duration"] = normalized_duration
        item["resolved_track_url"] = resolved_track_url
        item["audio_url"] = audio_url
        normalized_paths.append(normalized_path)

        if args.delay > 0 and index != len(tracks):
            time.sleep(args.delay)

    chapter_lines, total_duration = make_chapters(tracks)

    print()
    print("Chapters:")
    for line in chapter_lines:
        print(line)
    print(f"Total duration: {format_timestamp(total_duration)}")

    short_tracks = [item for item in tracks if item["duration"] < 10.0]
    if short_tracks:
        print()
        print(
            f"Warning: {len(short_tracks)} track(s) are under 10 seconds. "
            "The timestamps will still be in the description, but YouTube may not render all of them as clickable chapters."
        )

    print("Concatenating normalized tracks.")
    concat_audio(normalized_paths, combined_audio)

    print("Rendering full-album video.")
    render_album_video(combined_audio, video_frame_path, video_path)
    print(f"Rendered: {video_path}")

    rendered_duration = ffprobe_audio_duration(video_path)
    rendered_delta = abs(rendered_duration - total_duration)
    if rendered_delta > 2.0:
        raise RuntimeError(
            "Final video duration does not match generated chapter timing: "
            f"chapters={format_timestamp(total_duration)}, "
            f"video={format_timestamp(rendered_duration)}. Refusing to upload."
        )

    description = build_description(youtube_title, resolved_album_url, chapter_lines)

    if args.no_upload:
        description_path = outdir / f"{stem}_description.txt"
        description_path.write_text(description, encoding="utf-8")
        print("--no-upload used; skipping YouTube authentication/upload.")
        print(f"Video ready at: {video_path}")
        print(f"Description/chapters: {description_path}")
        return

    client_secret_path = pathlib.Path(args.client_secret).expanduser().resolve()
    token_path = pathlib.Path(args.token).expanduser().resolve()
    if not client_secret_path.exists():
        raise FileNotFoundError(f"Missing OAuth client secret file: {client_secret_path}")

    print("Authenticating YouTube account.")
    youtube = khi.get_youtube_client_manual_oauth(
        client_secret_path=client_secret_path,
        token_path=token_path,
    )

    print("Uploading to YouTube.")
    video_id = khi.upload_video(
        youtube=youtube,
        video_path=video_path,
        title=youtube_title,
        description=description,
        privacy=args.privacy,
    )

    print(f"YouTube video ID: {video_id}")

    try:
        khi.set_thumbnail(youtube, video_id, thumbnail_path)
        print("Thumbnail set.")
    except Exception as error:
        print(f"Warning: video uploaded, but thumbnail failed: {error}", file=sys.stderr)

    print(f"https://www.youtube.com/watch?v={video_id}")

    if args.keep_temp:
        print("Keeping temporary files because --keep-temp was used.")
        return

    remove_temp_tree(
        [
            raw_cover_path,
            video_frame_path,
            thumbnail_path,
            combined_audio,
            video_path,
        ],
        [track_dir],
    )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        raise SystemExit(130)
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1)

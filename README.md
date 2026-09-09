# KHInsider to YouTube Uploader

Python command-line tools for turning KHInsider soundtrack tracks or full albums into YouTube videos.

The project includes:

- `khinsider_to_youtube.py` — upload one track as `Game Name (OST) - Track Name`
- `khinsider_album_to_youtube.py` — combine an album into one video, preserve track order, and generate YouTube chapter timestamps

## Important

Only upload audio and artwork that you own or have permission to republish. This tool does not grant rights to music or images hosted by KHInsider. Use it in accordance with KHInsider's and YouTube's applicable terms.

## Disclaimer

This project was largely vibecoded so take everything with a grain of salt. I have confirmed that these work but audit the code if you have any qualms about running AI generated scripts on your personal machine.
## Features

### Single-track uploader

- Reads track and album metadata from a KHInsider track page
- Downloads the MP3 exposed by the track page
- Infers the game name and track name
- Produces titles in the form `Game Name (OST) - Track Name`
- Detects album cover art automatically
- Supports custom cover art with `--coverart`
- Renders a 1920x1080 still-image video with `ffmpeg`
- Creates a YouTube thumbnail
- Uploads through the YouTube Data API using OAuth
- Supports public, unlisted, and private uploads
- Supports `--no-upload` for local testing

### Full-album uploader

- Reads the public KHInsider album track list in page order
- Downloads each track through its individual track page
- Normalizes audio to matching AAC parameters and concatenates it in order
- Generates chapter timestamps from the resulting track durations
- Creates one full-album video and thumbnail
- Uses the album cover automatically, or an explicit `--coverart` URL
- Supports custom game name and complete YouTube title overrides
- Supports `--no-upload` and `--keep-temp`

## Requirements

System dependencies:

```bash
ffmpeg
ffprobe
```

On Arch Linux / CachyOS:

```bash
sudo pacman -S ffmpeg python-virtualenv
```

Python dependencies:

```bash
python -m pip install -r requirements.txt
```

## Install

```bash
git clone https://github.com/wed3/khinsider-youtube-uploader.git
cd khinsider-youtube-uploader

python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

## YouTube OAuth Setup

1. Create a Google Cloud project.
2. Enable **YouTube Data API v3**.
3. Configure the OAuth consent screen.
4. Add the scope:

   ```text
   https://www.googleapis.com/auth/youtube.upload
   ```

5. Create an OAuth client using the **Desktop app** application type.
6. Download the OAuth JSON file.
7. Rename it to `client_secret.json`.
8. Put it in the project directory.

The first upload walks through Google authorization and creates `token.json`.

**Do not commit `client_secret.json` or `token.json`.** Both are excluded by `.gitignore`.

## Single Track

Given a KHInsider track-page URL such as:

```text
https://downloads.khinsider.com/game-soundtracks/album/the-dark-rites-of-arkham-original-soundtrack-2026/02.%2520Arkham%25201933.mp3
```

upload it with:

```bash
python khinsider_to_youtube.py \
  'https://downloads.khinsider.com/game-soundtracks/album/the-dark-rites-of-arkham-original-soundtrack-2026/02.%2520Arkham%25201933.mp3' \
  --privacy public
```

Test the complete download/render process without uploading:

```bash
python khinsider_to_youtube.py 'KHINSIDER_TRACK_URL' --no-upload --keep-temp
```

Override the detected game name:

```bash
python khinsider_to_youtube.py 'KHINSIDER_TRACK_URL' \
  --game-name 'Exact Game Name'
```

Override the detected cover art:

```bash
python khinsider_to_youtube.py 'KHINSIDER_TRACK_URL' \
  --coverart 'https://example.com/cover.jpg'
```

`--cover-art` and `--cover-url` are accepted as aliases.

## Full Album

Use the normal public album URL:

```bash
python khinsider_album_to_youtube.py \
  'https://downloads.khinsider.com/game-soundtracks/album/my-summer-car-ost-2016' \
  --privacy public
```

The default YouTube title is:

```text
Game Name (OST) - Full Soundtrack
```

The generated description contains chapter timestamps in album order:

```text
Chapters:
0:00 Track One
2:31 Track Two
5:47 Track Three
```

Test without uploading:

```bash
python khinsider_album_to_youtube.py 'KHINSIDER_ALBUM_URL' \
  --no-upload --keep-temp
```

Use custom artwork:

```bash
python khinsider_album_to_youtube.py 'KHINSIDER_ALBUM_URL' \
  --coverart 'https://example.com/cover.jpg'
```

Override the inferred game name:

```bash
python khinsider_album_to_youtube.py 'KHINSIDER_ALBUM_URL' \
  --game-name 'Exact Game Name'
```

Override the entire YouTube title:

```bash
python khinsider_album_to_youtube.py 'KHINSIDER_ALBUM_URL' \
  --title 'My Exact Video Title'
```

### `/cp/add_album/<id>` links

KHInsider's `/cp/add_album/<id>` route is a login-gated mass-download action rather than the normal public album page. If it cannot be resolved automatically, the script asks for the corresponding public URL:

```text
https://downloads.khinsider.com/game-soundtracks/album/<album-slug>
```

You can also provide that explicitly:

```bash
python khinsider_album_to_youtube.py \
  'https://downloads.khinsider.com/cp/add_album/46444' \
  --album-url 'https://downloads.khinsider.com/game-soundtracks/album/example-album'
```

## Privacy

Both scripts accept:

```text
--privacy public
--privacy unlisted
--privacy private
```

The script default is `private` unless you specify another value.

## Temporary Files

Generated files are stored in `out/`. They are cleaned up after a successful run unless `--keep-temp` is supplied.

## Optional Bash Functions

Add these to `~/.bashrc`:

```bash
khinsider() {
  local app_dir="$HOME/khinsider-youtube-uploader"
  local python="$app_dir/.venv/bin/python"

  if [[ $# -lt 1 ]]; then
    echo "usage: khinsider <khinsider-track-url> [extra args]"
    return 2
  fi

  "$python" "$app_dir/khinsider_to_youtube.py" "$@"
}

khinsideralbum() {
  local app_dir="$HOME/khinsider-youtube-uploader"
  local python="$app_dir/.venv/bin/python"

  if [[ $# -lt 1 ]]; then
    echo "usage: khinsideralbum <khinsider-album-url> [extra args]"
    return 2
  fi

  "$python" "$app_dir/khinsider_album_to_youtube.py" "$@"
}
```

Reload the shell:

```bash
source ~/.bashrc
```

Examples:

```bash
khinsider 'KHINSIDER_TRACK_URL' --privacy public
khinsideralbum 'KHINSIDER_ALBUM_URL' --privacy public
```

## Cover Art

Cover art is fit inside a 16:9 black background without cropping or stretching. If automatic KHInsider cover detection fails, the scripts prompt for a direct image URL.

## License

MIT

"""Command-line downloader for images attached to YouTube Community posts.

YouTube's ytInitialData and image CDN URL formats are undocumented and may change.
Only public posts are supported; channel mode follows the public Posts feed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, urlsplit, urlunsplit

import requests


USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) "
    "Gecko/20100101 Firefox/128.0"
)

CONTENT_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/avif": ".avif",
    "image/bmp": ".bmp",
}


class PostError(Exception):
    """A post could not be parsed or its images could not be retrieved."""


def post_id_from_url(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or parsed.hostname not in {
        "youtube.com", "www.youtube.com", "m.youtube.com"
    }:
        raise PostError("Expected a youtube.com Community post URL")

    path_match = re.fullmatch(r"/post/([A-Za-z0-9_-]+)/?", parsed.path)
    post_id = (
        path_match.group(1)
        if path_match
        else parse_qs(parsed.query).get("lb", [None])[0]
    )
    if not post_id or not re.fullmatch(r"[A-Za-z0-9_-]+", post_id):
        raise PostError("Expected a /post/<id> URL or a URL with ?lb=<id>")
    return post_id


def initial_data_from_html(html: str) -> dict:
    """Read JSON assigned to ytInitialData, ignoring braces in embedded strings."""
    assignment = re.compile(
        r'(?:\b(?:var\s+)?ytInitialData\s*=\s*|'
        r'window\[[\'"]ytInitialData[\'"]\]\s*=\s*)'
    )
    decoder = json.JSONDecoder()
    for match in assignment.finditer(html):
        try:
            data, _ = decoder.raw_decode(html[match.end():].lstrip())
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    raise PostError("YouTube did not include parseable ytInitialData in the page")


def find_post(tree: object, wanted_id: str) -> dict | None:
    """Search YouTube's nested renderer structure for the exact post ID."""
    if isinstance(tree, dict):
        renderer = tree.get("backstagePostRenderer")
        if isinstance(renderer, dict) and renderer.get("postId") == wanted_id:
            return renderer
        for child in tree.values():
            result = find_post(child, wanted_id)
            if result is not None:
                return result
    elif isinstance(tree, list):
        for child in tree:
            result = find_post(child, wanted_id)
            if result is not None:
                return result
    return None


def image_urls_from_post(post: dict) -> list[str]:
    """Return one image URL per attachment, in original gallery order."""
    attachment = post.get("backstageAttachment") or {}
    multi = attachment.get("postMultiImageRenderer")
    if isinstance(multi, dict):
        items = multi.get("images") or []
    else:
        items = [attachment]

    urls: list[str] = []
    seen: set[str] = set()
    for item in items:
        renderer = item.get("backstageImageRenderer", {})
        thumbnails = renderer.get("image", {}).get("thumbnails", [])
        valid = [
            thumb for thumb in thumbnails
            if isinstance(thumb, dict) and isinstance(thumb.get("url"), str)
        ]
        if not valid:
            continue
        best = max(valid, key=lambda t: t.get("width", 0) * t.get("height", 0))
        url = best["url"]
        if url not in seen:
            urls.append(url)
            seen.add(url)
    return urls


def normalized_url(url: str) -> str:
    if url.startswith("//"):
        url = "https:" + url
    parsed = urlsplit(url)
    if parsed.scheme != "https":
        raise PostError(f"Unsupported image URL scheme: {url}")
    return url


def candidates_for_image(url: str) -> list[str]:
    """Try unbounded Google CDN sizes first, then YouTube's supplied thumbnail."""
    url = normalized_url(url)
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    if host == "ggpht.com" or host.endswith(".ggpht.com") or (
        host == "googleusercontent.com" or host.endswith(".googleusercontent.com")
    ):
        # Replace the entire suffix, including optional crop modifiers.
        resized_path = re.sub(r"=(?:s|w|h)\d+.*$", "=s0", parsed.path)
        if resized_path != parsed.path:
            base = urlunsplit((parsed.scheme, parsed.netloc, resized_path, "", ""))
            candidates = [base + "?imgmax=0", base, base[:-1] + "4000", url]
            return list(dict.fromkeys(candidates))
    return [url]


def extension_for_response(content_type: str, first_bytes: bytes) -> str:
    """Validate and derive the file extension from actual image data."""
    kind = content_type.split(";", 1)[0].strip().lower()
    if first_bytes.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if first_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if first_bytes[:4] == b"RIFF" and first_bytes[8:12] == b"WEBP":
        return ".webp"
    if first_bytes[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if first_bytes[4:12] in (b"ftypavif", b"ftypavis"):
        return ".avif"
    if first_bytes.startswith(b"BM"):
        return ".bmp"
    # Some CDN variants use newer formats. Trust image/*, never save HTML.
    if kind.startswith("image/"):
        return CONTENT_TYPES.get(kind, ".img")
    raise PostError(f"Unexpected response content type: {content_type or 'unknown'}")


def save_image(session: requests.Session, urls: list[str], destination: Path,
               index: int, timeout: float) -> Path:
    """Download atomically; if a candidate fails, try the next image URL."""
    errors: list[str] = []
    for candidate in urls:
        temp_path: Path | None = None
        try:
            with session.get(candidate, stream=True, timeout=timeout) as response:
                response.raise_for_status()
                chunks = response.iter_content(chunk_size=65536)
                first = next((chunk for chunk in chunks if chunk), b"")
                if not first:
                    raise PostError("Server returned an empty image")
                suffix = extension_for_response(
                    response.headers.get("Content-Type", ""), first
                )
                with tempfile.NamedTemporaryFile(
                    mode="wb", dir=destination, prefix=".yt-image-", delete=False
                ) as temporary:
                    temp_path = Path(temporary.name)
                    temporary.write(first)
                    for chunk in chunks:
                        if chunk:
                            temporary.write(chunk)
                path = destination / f"{index:02d}{suffix}"
                os.replace(temp_path, path)
                return path
        except (requests.RequestException, OSError, PostError) as error:
            errors.append(f"{candidate}: {error}")
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
    raise PostError("All image URLs failed:\n  " + "\n  ".join(errors))


def existing_image(destination: Path, index: int) -> Path | None:
    """Recognize previously downloaded, nonempty files at this gallery index."""
    if not destination.is_dir():
        return None
    for path in sorted(destination.glob(f"{index:02d}.*")):
        if path.suffix in set(CONTENT_TYPES.values()) | {".img"} and path.is_file() and path.stat().st_size:
            return path
    return None


def download_post_images(
    session: requests.Session, post: dict, destination: Path,
    timeout: float, force: bool = False, list_urls: bool = False,
) -> tuple[int, int, int]:
    """Process one post; return (saved images, skipped images, failed images)."""
    urls = image_urls_from_post(post)
    if list_urls:
        for url in urls:
            print(candidates_for_image(url)[0])
        return 0, 0, 0
    if not urls:
        return 0, 0, 0

    saved = skipped = failed = 0
    for index, image_url in enumerate(urls, start=1):
        if not force and existing_image(destination, index):
            skipped += 1
            continue
        destination.mkdir(parents=True, exist_ok=True)
        try:
            path = save_image(session, candidates_for_image(image_url),
                              destination, index, timeout)
            # Force-redownloading in another format should not leave both files.
            for old in destination.glob(f"{index:02d}.*"):
                if old != path and old.suffix in set(CONTENT_TYPES.values()) | {".img"}:
                    old.unlink()
            print(path)
            saved += 1
        except (PostError, OSError) as error:
            failed += 1
            print(f"{post['postId']} image {index} failed: {error}", file=sys.stderr)
    return saved, skipped, failed


def run(args: argparse.Namespace) -> int:
    """Dispatch based on URL type. Existing single-post output semantics remain."""
    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept-Language": "en-US,en;q=0.9",
    })
    from .channel import discover_channel_posts, resolve_channel_url

    try:
        post_id = post_id_from_url(args.url)
    except PostError:
        # Reject unsupported URLs with a useful error from channel validation.
        resolve_channel_url(args.url)
        post_id = None

    if post_id is not None:
        url = f"https://www.youtube.com/post/{post_id}"
        try:
            response = session.get(url, timeout=args.timeout)
            response.raise_for_status()
            tree = initial_data_from_html(response.text)
        except requests.RequestException as error:
            raise PostError(f"Could not fetch the post: {error}") from error
        post = find_post(tree, post_id)
        if post is None:
            raise PostError(
                "Post not found in page data. It may be private, unavailable, "
                "or YouTube may have changed its page format."
            )
        if not image_urls_from_post(post):
            raise PostError("No image attachments found in this post")
        output = args.output if args.output is not None else Path(post_id)
        saved, skipped, failed = download_post_images(
            session, post, output, args.timeout,
            force=getattr(args, "force", False), list_urls=args.list_urls,
        )
        if not args.list_urls:
            print(f"Saved {saved}, skipped {skipped}, failed {failed} images", file=sys.stderr)
        return 1 if failed else 0

    limit = getattr(args, "limit", 200)
    page_delay = getattr(args, "page_delay", 0.5)
    folder, posts = discover_channel_posts(
        session, args.url, limit, args.timeout, page_delay=page_delay,
    )
    base = (args.output if args.output is not None else Path.cwd()) / folder
    saved = skipped = failed = image_posts = 0
    for i, post in enumerate(posts, 1):
        urls = image_urls_from_post(post)
        if not urls:
            continue
        image_posts += 1
        if not args.list_urls:
            print(f"[{i}/{len(posts)}] {post['postId']}: {len(urls)} image(s)", file=sys.stderr)
        downloaded, already, errors = download_post_images(
            session, post, base / post["postId"], args.timeout,
            force=getattr(args, "force", False), list_urls=args.list_urls,
        )
        saved += downloaded
        skipped += already
        failed += errors
    if not args.list_urls:
        print(
            f"Channel summary: {len(posts)} posts, {image_posts} with images; "
            f"saved {saved}, skipped {skipped}, failed {failed}", file=sys.stderr,
        )
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="yt-community-images",
        description="Download full-size images from a YouTube post or recent channel posts.",
    )
    parser.add_argument("url", help="YouTube /post/<id> or channel /@handle/posts URL")
    parser.add_argument("-o", "--output", type=Path, metavar="DIR",
                        help="Single post: save directly in DIR; channel: DIR/@channel/<post-id>/")
    parser.add_argument("--limit", type=int, default=200, metavar="N",
                        help="Maximum channel posts to inspect (default: 200)")
    parser.add_argument("--force", action="store_true",
                        help="Redownload images that already exist")
    parser.add_argument("--list-urls", action="store_true",
                        help="Print proposed full-resolution URLs without downloading")
    parser.add_argument("--page-delay", type=float, default=0.5, metavar="SECS",
                        help="Delay between channel pagination requests (default: 0.5)")
    parser.add_argument("--timeout", type=float, default=30,
                        help="HTTP timeout in seconds (default: 30)")
    args = parser.parse_args(argv)
    if args.timeout <= 0 or args.limit <= 0 or args.page_delay < 0:
        parser.error("--timeout and --limit must be positive; --page-delay cannot be negative")
    if args.limit > 200:
        parser.error("--limit cannot exceed 200 in this version")
    try:
        return run(args)
    except (PostError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

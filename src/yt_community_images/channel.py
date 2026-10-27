"""Discover recent public YouTube channel posts via page JSON and continuations.

YouTube's web/Innertube structures are private and can change. This module does
not claim to expose posts hidden by YouTube's public Posts feed.
"""

from __future__ import annotations

import json
import re
import sys
import time
from urllib.parse import urlsplit, urlunsplit

import requests

from .cli import PostError, initial_data_from_html


POSTS_PARAMS = "EgVwb3N0c_IGBAoCSgA="
API_URL = "https://www.youtube.com/youtubei/v1/browse"


def channel_url_and_folder(url: str) -> tuple[str, str]:
    """Accept a public channel's Posts/Community tab URL; return URL and safe label."""
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in {
        "youtube.com", "www.youtube.com", "m.youtube.com"
    }:
        raise PostError("Expected a youtube.com channel Posts URL")
    components = parsed.path.strip("/").split("/")
    if len(components) != 2 or components[1] not in {"posts", "community"}:
        raise PostError("Expected a channel /posts or /community URL")
    segment = components[0]
    if re.fullmatch(r"@[A-Za-z0-9_.-]+", segment):
        folder = segment
    elif re.fullmatch(r"UC[A-Za-z0-9_-]+", segment):
        folder = segment
    else:
        # Channel IDs are normally /channel/UC..., handled below separately.
        raise PostError("Expected /@handle/posts or /channel/UC.../posts")
    return urlunsplit(("https", "www.youtube.com", parsed.path, "", "")), folder


def resolve_channel_url(url: str) -> tuple[str, str]:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in {
        "youtube.com", "www.youtube.com", "m.youtube.com"
    }:
        raise PostError("Expected a youtube.com channel Posts URL")
    path = parsed.path.strip("/").split("/")
    if (len(path) == 3 and path[0] == "channel" and
            re.fullmatch(r"UC[A-Za-z0-9_-]+", path[1]) and
            path[2] in {"posts", "community"}):
        return urlunsplit(("https", "www.youtube.com", parsed.path, "", "")), path[1]
    return channel_url_and_folder(url)


def iter_nodes(value: object):
    """Iteratively traverse dictionaries/lists to avoid recursive stack overflow."""
    stack = [value]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            yield node
            stack.extend(reversed(list(node.values())))
        elif isinstance(node, list):
            stack.extend(reversed(node))


def posts_from_tree(tree: object) -> list[dict]:
    """Pick out complete post renderers, preserving their first-seen order."""
    posts: list[dict] = []
    seen: set[str] = set()
    for node in iter_nodes(tree):
        post = node.get("backstagePostRenderer")
        if isinstance(post, dict):
            post_id = post.get("postId")
            if isinstance(post_id, str) and re.fullmatch(r"[A-Za-z0-9_-]+", post_id):
                if post_id not in seen:
                    posts.append(post)
                    seen.add(post_id)
    return posts


def continuation_from_tree(tree: object) -> str | None:
    """Find a genuine continuation-item token (not arbitrary tracking tokens)."""
    for node in iter_nodes(tree):
        item = node.get("continuationItemRenderer")
        if not isinstance(item, dict):
            continue
        endpoint = item.get("continuationEndpoint") or {}
        command = endpoint.get("continuationCommand") or {}
        token = command.get("token")
        if isinstance(token, str) and token:
            return token
    return None


def config_from_html(html: str) -> dict:
    """Parse inline ytcfg.set({...}) config without regex-matching nested JSON."""
    decoder = json.JSONDecoder()
    result: dict = {}
    for match in re.finditer(r"\bytcfg\.set\s*\(\s*", html):
        try:
            data, _ = decoder.raw_decode(html[match.end():])
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            result.update(data)
    return result


def _channel_id(tree: dict, config: dict) -> str | None:
    metadata = tree.get("metadata", {}).get("channelMetadataRenderer", {})
    candidate = metadata.get("externalId") or config.get("CHANNEL_ID")
    if isinstance(candidate, str) and re.fullmatch(r"UC[A-Za-z0-9_-]+", candidate):
        return candidate
    for node in iter_nodes(tree):
        header = node.get("c4TabbedHeaderRenderer", {})
        candidate = header.get("channelId") if isinstance(header, dict) else None
        if isinstance(candidate, str) and re.fullmatch(r"UC[A-Za-z0-9_-]+", candidate):
            return candidate
    return None


def _api_settings(config: dict) -> tuple[str, dict]:
    api_key = config.get("INNERTUBE_API_KEY")
    context = config.get("INNERTUBE_CONTEXT")
    if not isinstance(api_key, str) or not api_key:
        raise PostError("Page is missing YouTube's Innertube API key; cannot paginate")
    if not isinstance(context, dict) or not isinstance(context.get("client"), dict):
        # These values are ordinary context metadata, not credentials.
        context = {"client": {
            "clientName": "WEB", "clientVersion": config.get("INNERTUBE_CLIENT_VERSION", "2.20260101.00.00"),
            "hl": "en", "gl": "US",
        }}
    return api_key, context


def browse(session: requests.Session, config: dict, payload: dict, timeout: float) -> dict:
    key, context = _api_settings(config)
    try:
        response = session.post(
            API_URL, params={"key": key, "prettyPrint": "false"},
            json={"context": context, **payload}, timeout=timeout,
            headers={"Origin": "https://www.youtube.com", "Referer": "https://www.youtube.com/"},
        )
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise PostError(f"Could not retrieve YouTube channel feed: {exc}") from exc
    if not isinstance(data, dict):
        raise PostError("YouTube returned an unexpected response for the channel feed")
    return data


def discover_channel_posts(
    session: requests.Session, url: str, limit: int, timeout: float,
    page_delay: float = 0.5,
) -> tuple[str, list[dict]]:
    """Retrieve up to `limit` newest visible posts, including continuation pages.

    Returns (safe channel directory name, renderers). Raises on interrupted
    pagination rather than silently reporting a partial archive as complete.
    """
    page_url, folder = resolve_channel_url(url)
    try:
        response = session.get(page_url, timeout=timeout)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise PostError(f"Could not fetch channel Posts page: {exc}") from exc

    tree = initial_data_from_html(response.text)
    config = config_from_html(response.text)
    initial_posts = posts_from_tree(tree)
    token = continuation_from_tree(tree)

    # Some channels render the Posts tab only through the internal browse API.
    # If no posts/tokens were present in the initial HTML, try the browse tab.
    if not initial_posts and not token:
        channel_id = _channel_id(tree, config)
        if channel_id is None:
            raise PostError("Could not find channel Posts data or its channel ID")
        tree = browse(session, config, {"browseId": channel_id, "params": POSTS_PARAMS}, timeout)
        initial_posts = posts_from_tree(tree)
        token = continuation_from_tree(tree)

    collected: list[dict] = []
    seen_posts: set[str] = set()
    seen_tokens: set[str] = set()

    def append_posts(posts: list[dict]) -> None:
        for post in posts:
            post_id = post["postId"]
            if post_id not in seen_posts and len(collected) < limit:
                collected.append(post)
                seen_posts.add(post_id)

    append_posts(initial_posts)
    while token and len(collected) < limit:
        if token in seen_tokens:
            raise PostError("YouTube repeated a pagination token; feed appears truncated")
        seen_tokens.add(token)
        if page_delay:
            time.sleep(page_delay)
        page = browse(session, config, {"continuation": token}, timeout)
        posts = posts_from_tree(page)
        new_token = continuation_from_tree(page)
        old_count = len(collected)
        append_posts(posts)
        if not posts and not new_token:
            raise PostError("YouTube pagination returned no posts or continuation; stopping")
        if len(collected) == old_count and not new_token:
            break
        token = new_token

    print(f"Discovered {len(collected)} post(s) in channel feed", file=sys.stderr)
    return folder, collected

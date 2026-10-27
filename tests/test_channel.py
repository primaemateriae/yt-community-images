import argparse
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

from yt_community_images.channel import (
    POSTS_PARAMS, channel_url_and_folder, config_from_html,
    continuation_from_tree, discover_channel_posts, posts_from_tree,
    resolve_channel_url,
)
from yt_community_images.cli import PostError, run


def renderer(pid, images=1):
    return {"postId": pid, "backstageAttachment": {
        "postMultiImageRenderer": {"images": [
            {"backstageImageRenderer": {"image": {"thumbnails": [
                {"url": f"https://yt3.ggpht.com/{pid}_{i}=s640", "height": 640, "width": 640}
            ]}}} for i in range(images)
        ]}
    }}


def feed(*post_ids, next_token=None):
    items = [{"backstagePostThreadRenderer": {"post": {"backstagePostRenderer": renderer(pid)}}}
             for pid in post_ids]
    if next_token:
        items.append({"continuationItemRenderer": {"continuationEndpoint": {
            "continuationCommand": {"token": next_token}
        }}})
    return items


def html(posts, token=None):
    data = {"metadata": {"channelMetadataRenderer": {"externalId": "UCEXAMPLE123"}},
            "contents": {"twoColumnBrowseResultsRenderer": {"tabs": [{
                "tabRenderer": {"selected": True, "title": "Posts", "content": {
                    "sectionListRenderer": {"contents": [
                        {"itemSectionRenderer": {"contents": feed(*posts, next_token=token)}}
                    ]}
                }}
            }]}}}
    config = {"INNERTUBE_API_KEY": "TESTKEY", "INNERTUBE_CONTEXT": {
        "client": {"clientName": "WEB", "clientVersion": "1.2.3"}}}
    return ("<script>ytcfg.set(" + json.dumps(config) + ");"
            + "var ytInitialData = " + json.dumps(data) + ";</script>")


class FakeResponse:
    def __init__(self, data, image=False):
        self._data = data
        self.text = data if isinstance(data, str) else ""
        self.headers = {"Content-Type": "image/png" if image else "text/html"}

    def raise_for_status(self):
        pass

    def json(self):
        return self._data

    def iter_content(self, chunk_size=65536):
        yield self._data

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class Session:
    def __init__(self, initial, pages):
        self.initial = initial
        self.pages = pages
        self.headers = {}
        self.post_calls = []
        self.get_calls = []

    def get(self, url, **kwargs):
        self.get_calls.append(url)
        if url.startswith("https://www.youtube.com/"):
            return FakeResponse(self.initial)
        return FakeResponse(b"\x89PNG\r\n\x1a\nmock-image", image=True)

    def post(self, url, **kwargs):
        self.post_calls.append(kwargs)
        if "continuation" in kwargs["json"]:
            key = kwargs["json"]["continuation"]
        else:
            key = "browse"
        return FakeResponse(self.pages[key])


class TestChannelDiscovery(unittest.TestCase):
    def test_urls(self):
        self.assertEqual(resolve_channel_url("https://youtube.com/@hello/posts?view=0"),
                         ("https://www.youtube.com/@hello/posts", "@hello"))
        self.assertEqual(resolve_channel_url("https://youtube.com/channel/UCABCDEF/posts"),
                         ("https://www.youtube.com/channel/UCABCDEF/posts", "UCABCDEF"))
        self.assertEqual(resolve_channel_url("https://www.youtube.com/@hey/community"),
                         ("https://www.youtube.com/@hey/community", "@hey"))
        for value in ["https://evil.com/@hey/posts", "https://youtube.com/@hey/shorts",
                      "https://youtube.com/@hey/posts/../shorts"]:
            with self.assertRaises(PostError):
                resolve_channel_url(value)

    def test_parse(self):
        page = html(["UgkxONE", "UgkxTWO"], "NEXT")
        self.assertEqual(config_from_html(page)["INNERTUBE_API_KEY"], "TESTKEY")
        from yt_community_images.cli import initial_data_from_html
        tree = initial_data_from_html(page)
        self.assertEqual([p["postId"] for p in posts_from_tree(tree)], ["UgkxONE", "UgkxTWO"])
        self.assertEqual(continuation_from_tree(tree), "NEXT")

    def test_pagination_limit_dedup_and_context(self):
        sess = Session(html(["UgkxONE", "UgkxTWO"], "ONE"), {
            "ONE": {"onResponseReceivedEndpoints": [{"appendContinuationItemsAction": {
                "continuationItems": feed("UgkxTWO", "UgkxTHREE", "UgkxFOUR", next_token="TWO")
            }}]},
            "TWO": {"onResponseReceivedEndpoints": [{"appendContinuationItemsAction": {
                "continuationItems": feed("UgkxFIVE")
            }}]},
        })
        folder, posts = discover_channel_posts(sess, "https://youtube.com/@hello/posts", 4, 30, 0)
        self.assertEqual(folder, "@hello")
        self.assertEqual([p["postId"] for p in posts],
                         ["UgkxONE", "UgkxTWO", "UgkxTHREE", "UgkxFOUR"])
        self.assertEqual(len(sess.post_calls), 1)
        self.assertEqual(sess.post_calls[0]["json"]["continuation"], "ONE")
        self.assertEqual(sess.post_calls[0]["json"]["context"]["client"]["clientName"], "WEB")

    def test_browse_fallback(self):
        sess = Session(html([]), {
            "browse": {"contents": {"twoColumnBrowseResultsRenderer": {"tabs": [{
                "tabRenderer": {"selected": True, "title": "Posts", "content": {
                    "sectionListRenderer": {"contents": feed("UgkxFALLBACK")}
                }}
            }]}}}
        })
        _, posts = discover_channel_posts(sess, "https://youtube.com/@hello/posts", 200, 30, 0)
        self.assertEqual([p["postId"] for p in posts], ["UgkxFALLBACK"])
        self.assertEqual(sess.post_calls[0]["json"]["params"], POSTS_PARAMS)

    def test_repeat_token_fails_instead_of_silent_partial(self):
        sess = Session(html(["UgkxONE"], "AGAIN"), {
            "AGAIN": {"onResponseReceivedEndpoints": [{"appendContinuationItemsAction": {
                "continuationItems": feed("UgkxTWO", next_token="AGAIN")
            }}]},
        })
        with self.assertRaisesRegex(PostError, "repeated a pagination token"):
            discover_channel_posts(sess, "https://youtube.com/@hello/posts", 200, 30, 0)


class TestChannelDownload(unittest.TestCase):
    def args(self, output):
        return argparse.Namespace(
            url="https://www.youtube.com/@hello/posts", output=output,
            list_urls=False, timeout=30, limit=200, force=False, page_delay=0,
        )

    def test_default_path_skip_and_force(self):
        sess = Session(html(["UgkxONE", "UgkxTWO"]), {"browse": {}})
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp)
            args = self.args(output)
            with patch("yt_community_images.cli.requests.Session", return_value=sess):
                self.assertEqual(run(args), 0)
            self.assertTrue((output / "@hello" / "UgkxONE" / "01.png").is_file())
            self.assertTrue((output / "@hello" / "UgkxTWO" / "01.png").is_file())
            image_count = len([u for u in sess.get_calls if "ggpht.com" in u])
            with patch("yt_community_images.cli.requests.Session", return_value=sess):
                self.assertEqual(run(args), 0)
            self.assertEqual(len([u for u in sess.get_calls if "ggpht.com" in u]), image_count)
            args.force = True
            with patch("yt_community_images.cli.requests.Session", return_value=sess):
                self.assertEqual(run(args), 0)
            self.assertGreater(len([u for u in sess.get_calls if "ggpht.com" in u]), image_count)

    def test_list_urls_creates_no_dir(self):
        sess = Session(html(["UgkxONE"]), {"browse": {}})
        with tempfile.TemporaryDirectory() as temp:
            args = self.args(Path(temp))
            args.list_urls = True
            with patch("yt_community_images.cli.requests.Session", return_value=sess):
                with patch("sys.stdout", new_callable=io.StringIO) as result:
                    self.assertEqual(run(args), 0)
            self.assertIn("https://yt3.ggpht.com/UgkxONE_0=s0?imgmax=0", result.getvalue())
            self.assertFalse((Path(temp) / "@hello").exists())


if __name__ == "__main__":
    unittest.main()

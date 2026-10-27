import argparse
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from yt_community_images.cli import (
    PostError, candidates_for_image, extension_for_response,
    find_post, image_urls_from_post, initial_data_from_html,
    post_id_from_url, run, save_image,
)


POST_ID = "UgkxEXAMPLE"
SINGLE = {
    "postId": POST_ID,
    "backstageAttachment": {
        "backstageImageRenderer": {
            "image": {"thumbnails": [
                {"url": "https://yt3.ggpht.com/alpha=s100", "width": 100, "height": 100},
                {"url": "https://yt3.ggpht.com/alpha=s640", "width": 640, "height": 640},
            ]}
        }
    },
}
MULTI = {
    "postId": POST_ID,
    "backstageAttachment": {
        "postMultiImageRenderer": {
            "images": [
                {"backstageImageRenderer": {"image": {"thumbnails": [
                    {"url": "https://yt3.ggpht.com/a=s320", "width": 320, "height": 320}
                ]}}},
                {"backstageImageRenderer": {"image": {"thumbnails": [
                    {"url": "https://yt3.ggpht.com/b=s320", "width": 320, "height": 320}
                ]}}},
            ]
        }
    },
}


class FakeResponse:
    def __init__(self, content, kind="image/png", status=200):
        self.content = content
        self.text = content.decode() if kind == "text/html" else ""
        self.headers = {"Content-Type": kind}
        self.status = status

    def raise_for_status(self):
        if self.status >= 400:
            import requests
            raise requests.HTTPError(f"{self.status}")

    def iter_content(self, chunk_size=65536):
        yield self.content

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass


class TestParsing(unittest.TestCase):
    def test_post_url(self):
        self.assertEqual(post_id_from_url(f"https://youtube.com/post/{POST_ID}?feature=share"), POST_ID)
        self.assertEqual(post_id_from_url(f"https://youtube.com/@someone/community?lb={POST_ID}"), POST_ID)
        with self.assertRaises(PostError):
            post_id_from_url("https://notyoutube.com/post/UgkxEXAMPLE")

    def test_initial_data_with_braces_in_string(self):
        html = '<script>var ytInitialData = {"quote":"a { weird } string", "items": []};</script>'
        self.assertEqual(initial_data_from_html(html)["quote"], "a { weird } string")

    def test_find_post_and_single_image(self):
        doc = {"a": [1, {"backstagePostRenderer": SINGLE}]}
        self.assertIs(find_post(doc, POST_ID), SINGLE)
        self.assertEqual(image_urls_from_post(SINGLE), ["https://yt3.ggpht.com/alpha=s640"])

    def test_multi_image_order(self):
        self.assertEqual(image_urls_from_post(MULTI), [
            "https://yt3.ggpht.com/a=s320", "https://yt3.ggpht.com/b=s320",
        ])

    def test_maximum_resolution_with_crop_suffix(self):
        candidates = candidates_for_image(
            "https://yt3.ggpht.com/abc=s640-c-fcrop64=1,abcdef-nd-v1"
        )
        self.assertEqual(candidates[0], "https://yt3.ggpht.com/abc=s0?imgmax=0")
        self.assertEqual(candidates[1], "https://yt3.ggpht.com/abc=s0")
        self.assertEqual(candidates[2], "https://yt3.ggpht.com/abc=s4000")
        self.assertEqual(candidates[-1], "https://yt3.ggpht.com/abc=s640-c-fcrop64=1,abcdef-nd-v1")

    def test_other_image_cdn_keeps_path(self):
        url = "https://i.ytimg.com/vi/VIDEOID/hq720.jpg?sqp=abc"
        self.assertEqual(candidates_for_image(url), [url])

    def test_image_sniffing(self):
        self.assertEqual(extension_for_response("image/jpeg", b"\x89PNG\r\n\x1a\nxyz"), ".png")
        with self.assertRaises(PostError):
            extension_for_response("text/html", b"<html>")


class TestDownloads(unittest.TestCase):
    def test_download_fallback(self):
        class Session:
            def get(self, url, **kwargs):
                if "s0" in url:
                    return FakeResponse(b"nope", status=404)
                return FakeResponse(b"\x89PNG\r\n\x1a\nmock", "image/png")

        with tempfile.TemporaryDirectory() as tmp:
            path = save_image(
                Session(), ["https://yt3.ggpht.com/a=s0", "https://yt3.ggpht.com/a=s320"],
                Path(tmp), 1, 30,
            )
            self.assertEqual(path.name, "01.png")
            self.assertTrue(path.read_bytes().startswith(b"\x89PNG"))

    def test_default_output_omits_downloads_directory(self):
        """Downloads should land at ./<post-id>/, not ./downloads/<post-id>/."""
        class Session:
            headers = {}

            def get(self, url, **kwargs):
                if "youtube.com/post/" in url:
                    data = {"item": {"backstagePostRenderer": SINGLE}}
                    html = "<script>var ytInitialData = " + json.dumps(data) + ";</script>"
                    return FakeResponse(html.encode(), "text/html")
                return FakeResponse(b"\x89PNG\r\n\x1a\nmock", "image/png")

        args = argparse.Namespace(
            url=f"https://youtube.com/post/{POST_ID}",
            output=None, list_urls=False, timeout=30,
        )
        with tempfile.TemporaryDirectory() as tmp:
            original_cwd = os.getcwd()
            try:
                os.chdir(tmp)
                with patch("yt_community_images.cli.requests.Session", return_value=Session()):
                    self.assertEqual(run(args), 0)
                self.assertTrue((Path(POST_ID) / "01.png").is_file())
                self.assertFalse((Path("downloads") / POST_ID).exists())
            finally:
                os.chdir(original_cwd)

    def test_explicit_output_still_saves_directly_into_dir(self):
        """-o paths retain their existing semantics (no extra nesting)."""
        class Session:
            headers = {}

            def get(self, url, **kwargs):
                if "youtube.com/post/" in url:
                    data = {"item": {"backstagePostRenderer": SINGLE}}
                    html = "<script>var ytInitialData = " + json.dumps(data) + ";</script>"
                    return FakeResponse(html.encode(), "text/html")
                return FakeResponse(b"\x89PNG\r\n\x1a\nmock", "image/png")

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "pictures"
            args = argparse.Namespace(
                url=f"https://youtube.com/post/{POST_ID}",
                output=directory, list_urls=False, timeout=30,
            )
            with patch("yt_community_images.cli.requests.Session", return_value=Session()):
                self.assertEqual(run(args), 0)
            self.assertTrue((directory / "01.png").is_file())
            self.assertFalse((directory / POST_ID).exists())

    def test_list_urls_without_creating_directories(self):
        class Session:
            headers = {}

            def get(self, url, **kwargs):
                import json
                doc = {"item": {"backstagePostRenderer": SINGLE}}
                html = "<script>var ytInitialData = " + json.dumps(doc) + ";</script>"
                return FakeResponse(html.encode(), "text/html")

        args = argparse.Namespace(
            url=f"https://youtube.com/post/{POST_ID}",
            output=None, list_urls=True, timeout=30,
        )
        with patch("yt_community_images.cli.requests.Session", return_value=Session()):
            with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                result = run(args)
        self.assertEqual(result, 0)
        self.assertEqual(stdout.getvalue().strip(), "https://yt3.ggpht.com/alpha=s0?imgmax=0")


if __name__ == "__main__":
    unittest.main()

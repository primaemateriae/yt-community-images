# yt-community-images

Small Python CLI packaged as a Nix flake. Downloads the maximum publicly accessible
image renditions from either **one YouTube Community post** or **a channel's recent
Posts feed** (up to 200 discovered posts).

**No API key, sign-in, yt-dlp, or browser required.** It reads YouTube's public
web page data and, for channel continuation pages, uses the site's undocumented
`youtubei/v1/browse` interface. The latter is subject to change.

## Quick start

```sh
# Enter a shell with the executable
nix shell .
yt-community-images 'https://www.youtube.com/post/Ugkx...'
yt-community-images 'https://www.youtube.com/@SomeChannel/posts'

# Or run directly
nix run . -- 'https://www.youtube.com/@SomeChannel/posts'
```

The tool supports `https://www.youtube.com/@handle/posts`, `.../@handle/community`,
and `https://www.youtube.com/channel/UC.../posts` (or `/community`).
For individual posts, both `/post/<id>` and older `?lb=<id>` links work.

## Files and options

For **single posts** the default output stays as before, directly below the
working directory:

```text
./UgkxPOST_ID/
  01.jpg
  02.webp
```

`--output ./pictures` in single-post mode still puts files **directly** into
`./pictures/` rather than adding an extra post directory.

For **channel mode**, posts are organized within a directory named after the
channel handle or ID:

```text
./@SomeChannel/
  UgkxPOST_ID_1/
    01.jpg
    02.webp
  UgkxPOST_ID_2/
    01.png
```

`--output ./archive` in channel mode produces `./archive/@SomeChannel/<post-id>/`.
There is no intermediate `downloads/` directory.

```sh
# Discover at most the latest 50 posts (200 by default)
nix run . -- --limit 50 'https://www.youtube.com/@SomeChannel/posts'

# Save channel images below ./archive/@SomeChannel/
nix run . -- --output ./archive 'https://www.youtube.com/@SomeChannel/posts'

# Fetch new images; already-downloaded images are automatically skipped
nix run . -- 'https://www.youtube.com/@SomeChannel/posts'

# Overwrite already downloaded images
nix run . -- --force 'https://www.youtube.com/@SomeChannel/posts'

# Only print candidate maximum-resolution URLs, no files or directories created
nix run . -- --list-urls 'https://www.youtube.com/@SomeChannel/posts'

# Wait one second between channel feed pagination requests
nix run . -- --page-delay 1 'https://www.youtube.com/@SomeChannel/posts'

# Development and tests
nix develop
nix flake check
```

Additional flag: `--timeout SECONDS` (default: 30). Use `--help` for options.

## Channel discovery and incremental downloads

1. Fetches the channel Posts page and parses `ytInitialData`.
2. If needed, fetches the channel's Posts tab using YouTube's internal browse
   endpoint, extracting tokens from `continuationItemRenderer`.
3. Follows continuation tokens while the feed offers them, stopping after the
   requested number of posts or at the end of the feed. The maximum limit is 200.
4. Extracts attached images directly from the channel's post renderers (so it
   does not need to open each individual post).
5. Tries the CDN's unbounded image URL first, then fallback variants. Each
   successful image is written atomically to a numbered file, keeping its
   detected image format.
6. Skips already-existing nonempty image files by default. Failed or interrupted
   downloads can be retried by rerunning the same command; `--force` redownloads.

Text-only posts, polls, and video-only posts do not create output directories.
Failures are reported and the command exits nonzero rather than claiming a fully
successful download.

## Limitations

* **200 is a discovery cap, not a guarantee:** YouTube may expose fewer posts,
  block some requests, or omit older, deleted, private, members-only, or otherwise
  inaccessible posts. This is not a full channel history backup.
* The undocumented web/Innertube JSON and CDN URL conventions can change at
  any time; failed pagination is surfaced as an error.
* Images are the largest publicly served renditions obtainable by these URL
  variants, not necessarily the creator's original source files.
* No authentication or cookies are supported. A public browser page must expose
  suitable post data and Innertube configuration for pagination.
* "Skip existing" checks for nonempty image files by gallery position. Use
  `--force` when you want to refresh images after edits or changes in quality.

## Nix outputs

- `packages.<system>.default` — installable Python CLI (`nix shell .`)
- `apps.<system>.default` — `nix run .`
- `devShells.<system>.default` — `nix develop`
- `checks.<system>.unit-tests` — offline tests (`nix flake check`)

Supported targets: `x86_64-linux`, `aarch64-linux`, `x86_64-darwin`,
`aarch64-darwin`.

Nix may generate `flake.lock` on first use; commit it if you want to pin the
nixpkgs revision. When adding to a Git repository, `git add` the files before
running Nix so its Git flake snapshot includes them.

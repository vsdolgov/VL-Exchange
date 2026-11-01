# Attribution and branding: illustrative video

[Русский](branding-video.ru.md)

This 16-second synthetic clip illustrates how a receiving platform could show the authors of video analytics alongside its results. The first half names one fictional developer, Example Lab; the second names two, Example Lab and Vision Workshop. The names are taken from the [public VLMP example](attribution-channel-classifier/model-package.json).

- [Source video without overlays](branding-video-original.mp4)
- [Illustrative branded video](branding-video-overlay.mp4)
- [Rendering script](branding-video-render.py)

The scene, people, movements and detection boxes are generated drawings. There is no real person detector, no third-party platform integration, and no finished video-export product in this example. Labels such as “LIVE VIEW” and “EXPORT COPY” illustrate presentation modes. Receiving platforms need to implement those modes and support the experimental [Attribution & Branding profile](../../docs/en/ATTRIBUTION.md). An author's preference or license term cannot technically prevent a platform from removing a label. The original and branded video are separate files.

Both published videos are silent H.264/MP4, 1280 × 720, 20 fps. The script produces a new source and a separate branded copy, plus two PNG previews. It reads only the package's local fictional metadata and makes no network requests.

## Recreate the demonstration

Requires Python 3, Pillow, FFmpeg with `libx264`, and suitable system fonts (Arial on macOS or DejaVu Sans / Liberation Sans on Linux). Install Pillow in a virtual environment and run from the repository root:

```sh
python -m pip install 'Pillow>=10,<13'
python model/examples/branding-video-render.py --output-dir /tmp/vl-branding-demo
```

The output directory must be new or empty: the script refuses to overwrite existing files. If system fonts are unavailable, pass `--font /path/to/regular.ttf --bold-font /path/to/bold.ttf`. Make sure you have permission to use the chosen fonts. Output appearance and bytes can vary with fonts, Pillow and FFmpeg versions; byte-for-byte reproducibility is not claimed. These are illustrative assets, not performance or interoperability tests.

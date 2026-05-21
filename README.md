# Areena Scraper TUI

Terminal UI for selecting seasons and episodes from a Yle Areena series page and bulk-downloading them with consistent names.

## Requirements

- Python 3.10+
- `yt-dlp`
- `ffmpeg`

Both `yt-dlp` and `ffmpeg` are already available on this machine.

## Run

```bash
cd /home/b/areena-scraper
./areena_tui.py
```

You can also pass a URL as a shortcut:

```bash
./areena_tui.py 'https://areena.yle.fi/1-2523689'
```

Keyboard:

- `Up`/`Down` or `k`/`j`: move
- `Space`: toggle selection
- `a`: select all
- `n`: select none
- `Enter`: continue/start
- `q`: cancel, or exit the progress screen after completion

The default output directory is `~/Lataukset`. Files are named like:

```text
Oktonautit/Season 04/S04E01 - Erikoisjakso - Oktonautit ja suuri suoseikkailu.mp4
```

Options include quality, subtitles, download directory, parallel downloads, and dry-run mode.
Select `Download directory` in the options screen and press `Enter` to type a new folder.

The selected output folder, quality, subtitle setting, and parallel download count are saved to:

```text
~/.config/areena-scraper/config.json
```

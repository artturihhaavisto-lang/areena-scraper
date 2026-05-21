#!/usr/bin/env python3
import argparse
import curses
import html
import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path


USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/137 Safari/537.36"
AREENA_ORIGIN = "https://areena.yle.fi"
PLAYER_APP_ID = "player_static_prod"
PLAYER_APP_KEY = "8930d72170e48303cf5f3867780d549b"
CONFIG_PATH = Path.home() / ".config" / "areena-scraper" / "config.json"


@dataclass
class Season:
    title: str
    season_id: str
    selected: bool = True


@dataclass
class Episode:
    item_id: str
    title: str
    season_title: str
    season_number: int | None = None
    episode_number: int | None = None
    duration: str = ""
    selected: bool = True


@dataclass
class Options:
    quality: str = "best"
    subtitles: bool = True
    output_dir: str = str(Path.home() / "Lataukset")
    concurrency: int = 1
    dry_run: bool = False


def load_options() -> Options:
    options = Options()
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return options
    except Exception:
        return options

    if isinstance(data.get("quality"), str):
        options.quality = data["quality"]
    if isinstance(data.get("subtitles"), bool):
        options.subtitles = data["subtitles"]
    if isinstance(data.get("output_dir"), str) and data["output_dir"].strip():
        options.output_dir = os.path.expanduser(data["output_dir"].strip())
    if isinstance(data.get("concurrency"), int):
        options.concurrency = max(1, min(4, data["concurrency"]))
    if isinstance(data.get("dry_run"), bool):
        options.dry_run = data["dry_run"]
    return options


def save_options(options: Options) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(
        json.dumps(
            {
                "quality": options.quality,
                "subtitles": options.subtitles,
                "output_dir": options.output_dir,
                "concurrency": options.concurrency,
                "dry_run": options.dry_run,
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )


def request_text(url: str) -> str:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Origin": AREENA_ORIGIN,
            "Referer": f"{AREENA_ORIGIN}/",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8")


def request_json(url: str) -> dict:
    return json.loads(request_text(url))


def add_query(url: str, **params: str) -> str:
    parts = urllib.parse.urlsplit(url)
    replacements = {key: value for key, value in params.items() if value is not None}
    query = [
        (key, value)
        for key, value in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
        if key not in replacements
    ]
    query.extend(replacements.items())
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urllib.parse.urlencode(query), parts.fragment)
    )


def clean_filename(value: str) -> str:
    value = re.sub(r'[\\/:*?"<>|]+', " - ", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    return value or "Untitled"


def extract_next_data(page: str) -> dict:
    match = re.search(r'<script id="__NEXT_DATA__" type="application/json"[^>]*>(.*?)</script>', page)
    if not match:
        raise RuntimeError("Could not find Areena page data")
    return json.loads(html.unescape(match.group(1)))


def extract_env(page: str) -> dict:
    match = re.search(r"window\.envVariables = (\{.*?\});", page)
    if not match:
        raise RuntimeError("Could not find Areena frontend API keys")
    return json.loads(match.group(1))


def find_episode_lists(view: dict) -> list[dict]:
    lists = []
    for tab in view.get("tabs", []):
        if tab.get("slug") not in (None, "jaksot"):
            continue
        for block in tab.get("content", []):
            uri = block.get("source", {}).get("uri", "")
            has_season_filter = any(
                any("path.season" in opt.get("parameters", {}) for opt in filt.get("options", []))
                for filt in block.get("filters", [])
            )
            if block.get("type") == "list" and "content/list" in uri and has_season_filter:
                lists.append(block)
    return lists


def load_series(url: str) -> tuple[str, list[Season], str, str, str]:
    page = request_text(url)
    data = extract_next_data(page)
    env = extract_env(page)
    page_props = data["props"]["pageProps"]
    title = page_props["view"]["title"]
    lists = find_episode_lists(page_props["view"])
    if not lists:
        raise RuntimeError("Could not find an episode list on this Areena page")

    seasons: list[Season] = []
    seen = set()
    list_uri = lists[0]["source"]["uri"]
    for filt in lists[0].get("filters", []):
        for opt in filt.get("options", []):
            season_id = opt.get("parameters", {}).get("path.season")
            if season_id and season_id not in seen:
                seen.add(season_id)
                seasons.append(Season(opt.get("title", season_id), season_id))
    if not seasons:
        meta_item = page_props.get("meta", {}).get("item", {}).get("id", "")
        seasons.append(Season("Episodes", meta_item))

    return title, seasons, list_uri, env["appIdFrontend"], env["appKeyFrontend"]


def label_value(labels: list[dict], label_type: str) -> str:
    for label in labels:
        if label.get("type") == label_type:
            return label.get("formatted") or label.get("raw") or ""
    return ""


def fetch_episode_info(item_id: str) -> dict:
    url = (
        f"https://player.api.yle.fi/v1/preview/{item_id}.json"
        f"?app_id={PLAYER_APP_ID}&app_key={PLAYER_APP_KEY}"
    )
    data = request_json(url)["data"]
    return data.get("ongoing_ondemand") or data.get("ongoing_event") or {}


def load_episodes(list_uri: str, seasons: list[Season], app_id: str, app_key: str) -> list[Episode]:
    episodes: list[Episode] = []
    for season in seasons:
        offset = 0
        while True:
            url = add_query(
                list_uri,
                **{
                    "path.season": season.season_id,
                    "language": "fi",
                    "v": "10",
                    "client": "yle-areena-web",
                    "app_id": app_id,
                    "app_key": app_key,
                    "offset": str(offset),
                    "limit": "25",
                },
            )
            payload = request_json(url)
            rows = payload.get("data", [])
            for row in rows:
                pointer = row.get("pointer", {}).get("uri", "")
                match = re.search(r"items/(1-\d+)", pointer)
                if not match:
                    continue
                item_id = match.group(1)
                episode = Episode(
                    item_id=item_id,
                    title=row.get("title", item_id),
                    season_title=season.title,
                    duration=label_value(row.get("labels", []), "generic"),
                )
                try:
                    info = fetch_episode_info(item_id)
                    episode.season_number = int(info.get("season", {}).get("season_number") or 0) or None
                    episode.episode_number = int(info.get("episode_number") or 0) or None
                    episode.title = info.get("title", {}).get("fin") or episode.title
                except Exception:
                    pass
                episodes.append(episode)
            meta = payload.get("meta", {})
            offset += int(meta.get("limit") or len(rows) or 25)
            if not rows or offset >= int(meta.get("count") or len(rows)):
                break
    return dedupe_episodes(episodes)


def dedupe_episodes(episodes: list[Episode]) -> list[Episode]:
    seen = set()
    out = []
    for ep in episodes:
        if ep.item_id not in seen:
            seen.add(ep.item_id)
            out.append(ep)
    fallback_counts: dict[int | str, int] = {}
    for ep in out:
        season_key = ep.season_number or ep.season_title
        fallback_counts[season_key] = fallback_counts.get(season_key, 0) + 1
        if ep.episode_number is None:
            ep.episode_number = fallback_counts[season_key]
    return out


def draw_menu(stdscr, title: str, rows: list[str], selected: int, footer: str) -> None:
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    stdscr.addnstr(0, 0, title, w - 1, curses.A_BOLD)
    usable = max(1, h - 4)
    start = max(0, min(selected - usable // 2, max(0, len(rows) - usable)))
    for i, row in enumerate(rows[start : start + usable], start):
        attr = curses.A_REVERSE if i == selected else curses.A_NORMAL
        stdscr.addnstr(2 + i - start, 0, row, w - 1, attr)
    stdscr.addnstr(h - 1, 0, footer, w - 1, curses.A_DIM)
    stdscr.refresh()


def controls_footer(options: Options, action: str) -> str:
    return f"Space toggle  a all  n none  d directory  Enter {action}  q quit  dir: {options.output_dir}"


def season_screen(stdscr, series_title: str, seasons: list[Season], options: Options) -> bool:
    selected = 0
    while True:
        rows = [f"[{'x' if s.selected else ' '}] {s.title} ({s.season_id})" for s in seasons]
        draw_menu(stdscr, f"Areena: {series_title} - seasons", rows, selected, controls_footer(options, "continue"))
        key = stdscr.getch()
        if key in (curses.KEY_UP, ord("k")):
            selected = max(0, selected - 1)
        elif key in (curses.KEY_DOWN, ord("j")):
            selected = min(len(seasons) - 1, selected + 1)
        elif key == ord(" "):
            seasons[selected].selected = not seasons[selected].selected
        elif key == ord("a"):
            for season in seasons:
                season.selected = True
        elif key == ord("n"):
            for season in seasons:
                season.selected = False
        elif key == ord("d"):
            options.output_dir = prompt_output_dir(stdscr, options.output_dir)
            save_options(options)
        elif key in (10, 13):
            return any(s.selected for s in seasons)
        elif key == ord("q"):
            return False


def episode_screen(stdscr, episodes: list[Episode], options: Options) -> bool:
    selected = 0
    while True:
        rows = []
        for ep in episodes:
            code = ""
            if ep.season_number and ep.episode_number:
                code = f"S{ep.season_number:02d}E{ep.episode_number:02d} "
            rows.append(f"[{'x' if ep.selected else ' '}] {code}{ep.title} [{ep.item_id}]")
        draw_menu(stdscr, "Episodes", rows, selected, controls_footer(options, "options"))
        key = stdscr.getch()
        if key in (curses.KEY_UP, ord("k")):
            selected = max(0, selected - 1)
        elif key in (curses.KEY_DOWN, ord("j")):
            selected = min(len(episodes) - 1, selected + 1)
        elif key == ord(" "):
            episodes[selected].selected = not episodes[selected].selected
        elif key == ord("a"):
            for ep in episodes:
                ep.selected = True
        elif key == ord("n"):
            for ep in episodes:
                ep.selected = False
        elif key == ord("d"):
            options.output_dir = prompt_output_dir(stdscr, options.output_dir)
            save_options(options)
        elif key in (10, 13):
            return any(ep.selected for ep in episodes)
        elif key == ord("q"):
            return False


def prompt_output_dir(stdscr, current: str) -> str:
    curses.curs_set(1)
    curses.echo()
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    stdscr.addnstr(0, 0, "Download directory", w - 1, curses.A_BOLD)
    stdscr.addnstr(2, 0, f"Current: {current}", w - 1)
    stdscr.addnstr(4, 0, "Enter new directory, or leave empty to keep current.", w - 1, curses.A_DIM)
    stdscr.addstr(6, 0, "Directory: ")
    stdscr.refresh()
    value = stdscr.getstr(6, 11, max(1, w - 12)).decode().strip()
    curses.noecho()
    curses.curs_set(0)
    return os.path.abspath(os.path.expanduser(value)) if value else current


def options_screen(stdscr, options: Options, count: int) -> bool:
    fields = ["Quality", "Subtitles", "Download directory", "Concurrency", "Dry run", "Start"]
    selected = 0
    while True:
        rows = [
            f"Quality: {options.quality}",
            f"Download subtitles: {'yes' if options.subtitles else 'no'}",
            f"Download directory: {options.output_dir}",
            f"Parallel downloads: {options.concurrency}",
            f"Dry run only: {'yes' if options.dry_run else 'no'}",
            f"Start download queue ({count} selected)",
        ]
        draw_menu(stdscr, "Download options", rows, selected, "Enter edit/toggle/start  arrows move  q cancel")
        key = stdscr.getch()
        if key in (curses.KEY_UP, ord("k")):
            selected = max(0, selected - 1)
        elif key in (curses.KEY_DOWN, ord("j")):
            selected = min(len(fields) - 1, selected + 1)
        elif key == ord("q"):
            return False
        elif key in (10, 13, curses.KEY_RIGHT, ord(" ")):
            if selected == 0:
                choices = ["best", "1080", "720", "540", "360", "worst"]
                options.quality = choices[(choices.index(options.quality) + 1) % len(choices)]
            elif selected == 1:
                options.subtitles = not options.subtitles
            elif selected == 2:
                options.output_dir = prompt_output_dir(stdscr, options.output_dir)
                save_options(options)
            elif selected == 3:
                options.concurrency = 1 if options.concurrency >= 4 else options.concurrency + 1
            elif selected == 4:
                options.dry_run = not options.dry_run
            elif selected == 5:
                save_options(options)
                return True


def format_selector(quality: str) -> str:
    if quality == "best":
        return "bestvideo+bestaudio/best"
    if quality == "worst":
        return "worstvideo+worstaudio/worst"
    return f"bestvideo[height<={quality}]+bestaudio/best[height<={quality}]"


def output_template(series: str, ep: Episode) -> str:
    season_number = ep.season_number or parse_int(ep.season_title) or 0
    prefix = ""
    if season_number and ep.episode_number:
        prefix = f"S{season_number:02d}E{ep.episode_number:02d} - "
    elif season_number:
        prefix = f"S{season_number:02d} - "
    season_dir = f"Season {season_number:02d}" if season_number else clean_filename(ep.season_title)
    return str(Path(clean_filename(series)) / season_dir / f"{prefix}{clean_filename(ep.title)}.%(ext)s")


def parse_int(value: str) -> int | None:
    match = re.search(r"\d+", value or "")
    return int(match.group(0)) if match else None


def download_worker(series: str, options: Options, jobs: queue.Queue, events: queue.Queue) -> None:
    while True:
        try:
            ep = jobs.get_nowait()
        except queue.Empty:
            return
        rel_template = output_template(series, ep)
        url = f"https://areena.yle.fi/{ep.item_id}"
        if options.dry_run:
            events.put(("done", ep.item_id, f"DRY {rel_template}"))
            jobs.task_done()
            continue
        cmd = [
            shutil.which("yt-dlp") or "yt-dlp",
            "--newline",
            "--no-part",
            "-f",
            format_selector(options.quality),
            "-o",
            str(Path(options.output_dir) / rel_template),
            url,
        ]
        if options.subtitles:
            cmd[1:1] = ["--write-subs", "--sub-langs", "fi,fin", "--convert-subs", "srt"]
        events.put(("start", ep.item_id, ep.title))
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        last = ""
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.strip()
            if line:
                last = line
                events.put(("progress", ep.item_id, line))
        rc = proc.wait()
        if rc == 0:
            events.put(("done", ep.item_id, last or "done"))
        else:
            events.put(("error", ep.item_id, last or f"yt-dlp exited {rc}"))
        jobs.task_done()


def progress_screen(stdscr, series: str, episodes: list[Episode], options: Options) -> None:
    jobs: queue.Queue = queue.Queue()
    events: queue.Queue = queue.Queue()
    selected = [ep for ep in episodes if ep.selected]
    status = {ep.item_id: "queued" for ep in selected}
    for ep in selected:
        jobs.put(ep)
    for _ in range(max(1, min(options.concurrency, len(selected)))):
        threading.Thread(target=download_worker, args=(series, options, jobs, events), daemon=True).start()
    done = 0
    log: list[str] = []
    while done < len(selected):
        try:
            kind, item_id, message = events.get(timeout=0.2)
            if kind in ("done", "error"):
                done += 1
            status[item_id] = f"{kind}: {message}"
            log.append(f"{item_id} {kind}: {message}")
            log = log[-200:]
        except queue.Empty:
            pass
        stdscr.erase()
        h, w = stdscr.getmaxyx()
        stdscr.addnstr(0, 0, f"{series} - {done}/{len(selected)} complete", w - 1, curses.A_BOLD)
        row = 2
        for ep in selected[: max(1, h // 2 - 2)]:
            line = f"{ep.item_id} {ep.title}: {status.get(ep.item_id, '')}"
            stdscr.addnstr(row, 0, line, w - 1)
            row += 1
        stdscr.addnstr(row + 1, 0, "Recent output", w - 1, curses.A_BOLD)
        for line in log[-max(1, h - row - 4) :]:
            row += 1
            stdscr.addnstr(row, 0, line, w - 1, curses.A_DIM)
        stdscr.addnstr(h - 1, 0, "Press q after completion", w - 1, curses.A_DIM)
        stdscr.refresh()
    while stdscr.getch() != ord("q"):
        pass


def prompt_url(stdscr) -> str:
    curses.curs_set(1)
    curses.echo()
    stdscr.erase()
    stdscr.addstr(0, 0, "Yle Areena series URL")
    stdscr.addstr(2, 0, "URL: ")
    stdscr.refresh()
    value = stdscr.getstr(2, 5, 300).decode().strip()
    curses.noecho()
    curses.curs_set(0)
    if not value:
        raise RuntimeError("No URL entered")
    if not value.startswith(("http://", "https://")):
        value = f"https://areena.yle.fi/{value.lstrip('/')}"
    return value


def app(stdscr, url: str | None) -> None:
    curses.curs_set(0)
    stdscr.nodelay(False)
    if not url:
        url = prompt_url(stdscr)
    stdscr.addstr(0, 0, "Loading Areena series...")
    stdscr.refresh()
    series, seasons, list_uri, app_id, app_key = load_series(url)
    options = load_options()
    if not season_screen(stdscr, series, seasons, options):
        return
    chosen_seasons = [s for s in seasons if s.selected]
    stdscr.erase()
    stdscr.addstr(0, 0, "Loading episode list...")
    stdscr.refresh()
    episodes = load_episodes(list_uri, chosen_seasons, app_id, app_key)
    if not episodes or not episode_screen(stdscr, episodes, options):
        return
    if not options_screen(stdscr, options, len([ep for ep in episodes if ep.selected])):
        return
    Path(options.output_dir).mkdir(parents=True, exist_ok=True)
    progress_screen(stdscr, series, episodes, options)


def main() -> None:
    parser = argparse.ArgumentParser(description="TUI downloader for Yle Areena series pages")
    parser.add_argument(
        "url",
        nargs="?",
        help="Optional Areena series URL shortcut, for example https://areena.yle.fi/1-2523689",
    )
    args = parser.parse_args()
    if not shutil.which("yt-dlp"):
        raise SystemExit("yt-dlp was not found in PATH")
    curses.wrapper(app, args.url)


if __name__ == "__main__":
    main()

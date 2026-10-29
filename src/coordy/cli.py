"""`coordy` command line."""

from __future__ import annotations

import argparse
import csv
import logging
import sys
import time
from pathlib import Path

from . import __version__
from .config import load_settings


def _settings():
    return load_settings()


def _engine(settings):
    from .pipeline import Engine

    return Engine(settings)


def _stage_printer():
    last = {"name": None, "pct": -1}

    def stage(name: str, frac: float | None = None) -> None:
        pct = int((frac or 0) * 100)
        if name != last["name"] or pct >= last["pct"] + 10:
            print(f"  {name}" + (f" {pct}%" if frac is not None else ""), flush=True)
            last.update(name=name, pct=pct)

    return stage


def cmd_init(args) -> int:
    settings = _settings()
    p = settings.paths
    print(f"Coord home ready: {p.root}")
    print(f"  Drop recordings in : {p.inbox}")
    print(f"  Edit names to hide : {p.aliases}")
    print(f"  Edit settings      : {p.config}")
    print(f"  Edit glossary      : {p.glossary}")
    return 0


def cmd_doctor(args) -> int:
    from .doctor import run

    return run(_settings())


def cmd_setup_models(args) -> int:
    settings = _settings()
    if settings.privacy.offline:
        print("privacy.offline is true - set it to false in config.toml to download models.")
        return 1
    from faster_whisper.utils import download_model

    print(f"1/3 Whisper '{settings.whisper.model}' (about 3 GB the first time)...", flush=True)
    print(f"    saved in {download_model(settings.whisper.model)}")

    if settings.diarization.enabled:
        print(f"2/3 Speaker model '{settings.diarization.model}'...", flush=True)
        from .diarize import Diarizer

        try:
            Diarizer(settings.diarization, settings.hf_token).pipeline()
            print("    ok")
        except Exception as exc:  # noqa: BLE001
            print(f"    FAILED: {exc}")
            print("    Accept the terms at https://huggingface.co/" + settings.diarization.model)
            print("    and put a read token in config.toml -> [diarization] hf_token")
    else:
        print("2/3 Speaker detection disabled - skipped")

    from .llm import Ollama

    print(f"3/3 Ollama model '{settings.llm.model}'...", flush=True)
    try:
        llm = Ollama(settings.llm, settings.privacy)
        if llm.has_model():
            print("    already installed")
        else:
            seen = set()

            def show(event: dict) -> None:
                status = event.get("status", "")
                if status not in seen:
                    seen.add(status)
                    print(f"    {status}", flush=True)

            llm.pull(progress=show)
    except Exception as exc:  # noqa: BLE001
        print(f"    FAILED: {exc} - is Ollama installed and running?")
    print("\nDone. Set privacy.offline = true in config.toml to block further downloads.")
    return 0


def cmd_process(args) -> int:
    settings = _settings()
    engine = _engine(settings)
    for f in args.files:
        print(f"Processing {f}")
        mid = engine.process(Path(f), title=args.title, project=args.project, day=args.date, stage=_stage_printer())
        print(f"Done: {mid}\n  minutes: {settings.paths.minutes / (mid + '.md')}")
    return 0


def cmd_watch(args) -> int:
    from .pipeline import inbox_files

    settings = _settings()
    engine = _engine(settings)
    print(f"Watching {settings.paths.inbox} (Ctrl+C to stop)")
    sizes: dict[Path, int] = {}
    failed: set[Path] = set()
    while True:
        for path in inbox_files(settings, engine.store):
            if path in failed:
                continue
            size = path.stat().st_size
            # Only start once the file stopped growing (recording finished / copy complete).
            if sizes.get(path) != size:
                sizes[path] = size
                continue
            print(f"New recording: {path.name}")
            try:
                mid = engine.process(path, project=args.project, stage=_stage_printer())
                print(f"Done: {mid}")
            except Exception as exc:  # noqa: BLE001
                failed.add(path)
                print(f"FAILED {path.name}: {exc}")
        time.sleep(args.interval)


def cmd_list(args) -> int:
    from .store import Store

    store = Store(_settings().paths.db)
    for m in store.meetings(project=args.project, limit=args.limit):
        print(f"{m['id']:<45} {m['status']:<12} {m['project'] or '-':<15} {m['title']}")
    return 0


def cmd_speakers(args) -> int:
    settings = _settings()
    engine = _engine(settings)
    mapping = dict(pair.split("=", 1) for pair in args.mapping)
    if mapping:
        print(engine.set_speakers(args.meeting, mapping))
    meeting = engine.store.meeting(args.meeting)
    counts: dict[str, int] = {}
    for seg in engine.store.segments(args.meeting):
        counts[seg["speaker"]] = counts.get(seg["speaker"], 0) + 1
    print("Speaker  lines  code")
    for spk, n in sorted(counts.items()):
        print(f"{spk or '?':<8} {n:>5}  {meeting['speakers'].get(spk, '-')}")
    print("\nSet with: coordy speakers <meeting> S1=P01 S2=P02, then: coordy minutes <meeting>")
    return 0


def cmd_minutes(args) -> int:
    settings = _settings()
    engine = _engine(settings)
    engine.summarise(args.meeting, stage=_stage_printer())
    print(f"Minutes: {settings.paths.minutes / (args.meeting + '.md')}")
    return 0


def cmd_redact(args) -> int:
    settings = _settings()
    engine = _engine(settings)
    ids = [m["id"] for m in engine.store.meetings(limit=100000)] if args.all else args.meetings
    for mid in ids:
        if not (settings.paths.raw / f"{mid}.json").exists():
            print(f"{mid}: no raw transcript, skipped")
            continue
        hits = engine.redact(mid)
        print(f"{mid}: replaced {sum(hits.values())} name(s) {hits or ''}")
    print("Run `coordy minutes <meeting>` to refresh minutes with the new redaction.")
    return 0


def cmd_named(args) -> int:
    settings = _settings()
    out = _engine(settings).write_named_minutes(args.meeting)
    print(f"Minutes with real names (keep local): {out}")
    return 0


def cmd_export(args) -> int:
    from .redact import Redactor
    from .store import Store

    settings = _settings()
    store = Store(settings.paths.db)
    redactor = Redactor.from_settings(settings)
    rows = store.items(kind=args.kind, status=args.status, project=args.project, limit=1000000)
    cols = ["id", "meeting_date", "meeting_title", "project", "kind", "text", "owner", "due", "discipline", "status", "note", "timestamp", "meeting_id"]
    out = Path(args.output)
    # utf-8-sig so Excel shows Arabic correctly.
    with out.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            if args.real_names:
                r = {k: redactor.unredact(v) if isinstance(v, str) else v for k, v in r.items()}
            w.writerow(r)
    print(f"{len(rows)} items -> {out}")
    return 0


def cmd_suggest_aliases(args) -> int:
    """Ask the LOCAL model which names in a transcript are not hidden yet."""
    from .llm import Ollama
    from .minutes import chunk_lines, transcript_lines
    from .store import Store

    settings = _settings()
    store = Store(settings.paths.db)
    meeting = store.meeting(args.meeting)
    lines = transcript_lines(store.segments(args.meeting), meeting["speakers"])
    llm = Ollama(settings.llm, settings.privacy)
    schema = {
        "type": "object",
        "properties": {"names": {"type": "array", "items": {"type": "object", "properties": {
            "text": {"type": "string"}, "kind": {"type": "string", "enum": ["person", "org", "project", "term"]}},
            "required": ["text", "kind"]}}},
        "required": ["names"],
    }
    system = ("List every personal name, company/organisation name, and project or site name that appears in "
              "this transcript (Arabic or English), exactly as written. Ignore codes like P01 or ORG01.")
    found: dict[str, str] = {}
    for chunk in chunk_lines(lines, settings.llm.chunk_chars):
        for n in llm.chat_json(system, chunk, schema).get("names", []):
            found.setdefault(n["text"].strip(), n["kind"])
    if not found:
        print("No unhidden names found.")
        return 0
    print("Possible names still visible (add the real ones to aliases.toml, then `coordy redact --all`):")
    for text, kind in sorted(found.items()):
        print(f"  [{kind}] {text}")
    return 0


def cmd_claude_config(args) -> int:
    from .claude_desktop import snippet, write

    if args.write:
        for p in write():
            print(f"Updated {p} (backup saved next to it)")
        print("Restart Claude Desktop completely (quit from the tray icon) to load Coord.")
    else:
        print("Add this to claude_desktop_config.json (Claude Desktop > Settings > Developer > Edit Config):\n")
        print(snippet())
        print("\nOr run: coordy claude-config --write")
    return 0


def cmd_mcp(args) -> int:
    from .mcp_server import run

    run()
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="coordy", description="Coord - your private AI coordination assistant.")
    p.add_argument("--version", action="version", version=f"coordy {__version__}")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create the Coordy folder and editable templates").set_defaults(func=cmd_init)
    sub.add_parser("doctor", help="check GPU, models, Ollama and Claude Desktop setup").set_defaults(func=cmd_doctor)
    sub.add_parser("setup-models", help="download Whisper, speaker and Ollama models once").set_defaults(func=cmd_setup_models)

    s = sub.add_parser("process", help="transcribe + redact + extract minutes for recordings")
    s.add_argument("files", nargs="+")
    s.add_argument("--title", default="")
    s.add_argument("--project", default="")
    s.add_argument("--date", default="", help="YYYY-MM-DD (default: from file name or file date)")
    s.set_defaults(func=cmd_process)

    s = sub.add_parser("watch", help="process new recordings dropped in the inbox folder")
    s.add_argument("--project", default="")
    s.add_argument("--interval", type=int, default=15, help="seconds between checks")
    s.set_defaults(func=cmd_watch)

    s = sub.add_parser("list", help="list meetings")
    s.add_argument("--project", default="")
    s.add_argument("--limit", type=int, default=50)
    s.set_defaults(func=cmd_list)

    s = sub.add_parser("speakers", help="show / set speaker codes, e.g. S1=P01")
    s.add_argument("meeting")
    s.add_argument("mapping", nargs="*")
    s.set_defaults(func=cmd_speakers)

    s = sub.add_parser("minutes", help="re-run minutes extraction for a meeting")
    s.add_argument("meeting")
    s.set_defaults(func=cmd_minutes)

    s = sub.add_parser("redact", help="re-apply aliases.toml to stored transcripts")
    s.add_argument("meetings", nargs="*")
    s.add_argument("--all", action="store_true")
    s.set_defaults(func=cmd_redact)

    s = sub.add_parser("named", help="write minutes with real names restored (local file)")
    s.add_argument("meeting")
    s.set_defaults(func=cmd_named)

    s = sub.add_parser("export", help="export the decision/action register to CSV (opens in Excel)")
    s.add_argument("output", nargs="?", default="coordy-register.csv")
    s.add_argument("--kind", default="")
    s.add_argument("--status", default="")
    s.add_argument("--project", default="")
    s.add_argument("--real-names", action="store_true", help="restore real names (keep the file private)")
    s.set_defaults(func=cmd_export)

    s = sub.add_parser("suggest-aliases", help="find names not yet hidden (local model)")
    s.add_argument("meeting")
    s.set_defaults(func=cmd_suggest_aliases)

    s = sub.add_parser("claude-config", help="register Coord in Claude Desktop")
    s.add_argument("--write", action="store_true", help="edit the config file (a backup is made)")
    s.set_defaults(func=cmd_claude_config)

    sub.add_parser("mcp", help="run the MCP server (started by Claude Desktop)").set_defaults(func=cmd_mcp)
    return p


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except AttributeError:
            pass
    args = build_parser().parse_args(argv)
    if args.command != "mcp":
        logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    try:
        return args.func(args) or 0
    except KeyboardInterrupt:
        return 130
    except (FileNotFoundError, KeyError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

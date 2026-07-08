#!/usr/bin/env python3
"""Generate per-project changelogs from recent TwiCC session activity."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit


WINDOW_RE = re.compile(r"^(?P<count>\d+)(?P<unit>h|d)$")
ACTIVITY_FIELDS = (
    "last_new_content_at",
    "last_updated_at",
    "last_started_at",
    "created_at",
)
MAX_TEXT_CHARS = 80_000


@dataclass
class RepoActivity:
    local_path: Path
    remote_url: str | None
    sessions: int = 0
    projects: set[str] = field(default_factory=set)
    branches: set[str] = field(default_factory=set)
    latest_activity: datetime | None = None


@dataclass(frozen=True)
class GitContext:
    text: str
    has_contribution: bool


def parse_window(value: str) -> timedelta:
    match = WINDOW_RE.match(value.strip())
    if match is None:
        raise argparse.ArgumentTypeError("window must look like 24h or 7d")

    count = int(match.group("count"))
    unit = match.group("unit")
    if count <= 0:
        raise argparse.ArgumentTypeError("window must be greater than zero")

    if unit == "h":
        return timedelta(hours=count)
    return timedelta(days=count)


def parse_timestamp(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=UTC)
    if not isinstance(value, str) or not value:
        return None

    normalized = value.removesuffix("Z") + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def session_activity_at(session: dict[str, Any]) -> datetime | None:
    for field_name in ACTIVITY_FIELDS:
        parsed = parse_timestamp(session.get(field_name))
        if parsed is not None:
            return parsed
    return parse_timestamp(session.get("mtime"))


def resolve_executable(value: str | None, *, env_name: str, default: str) -> list[str]:
    raw = value or os.environ.get(env_name) or default
    parts = raw.split()
    if not parts:
        raise RuntimeError(f"{env_name} is empty")

    if os.sep in parts[0] or parts[0].startswith("."):
        if not Path(parts[0]).exists():
            raise RuntimeError(f"executable not found: {parts[0]}")
        return parts

    resolved = shutil.which(parts[0])
    if resolved is None:
        option = env_name.lower().replace("_", "-")
        raise RuntimeError(f"executable not found: {parts[0]}; pass --{option}")
    return [resolved, *parts[1:]]


def run_json(command: list[str]) -> Any:
    result = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"command failed: {' '.join(command)}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"command did not return JSON: {' '.join(command)}") from exc


def run_text(command: list[str], *, cwd: Path | None = None, check: bool = False) -> str:
    result = subprocess.run(
        command,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if check and result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit code {result.returncode}"
        raise RuntimeError(f"{' '.join(command)} failed: {detail}")
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def load_sessions(
    *,
    twicc: list[str],
    page_size: int,
    include_hidden: bool,
    include_archived: bool,
    project: str | None,
    workspace: str | None,
) -> list[dict[str, Any]]:
    sessions: list[dict[str, Any]] = []
    offset = 0

    while True:
        command = [*twicc, "sessions", "--limit", str(page_size), "--offset", str(offset)]
        if include_hidden:
            command.append("--include-hidden")
        if include_archived:
            command.append("--include-archived")
        if project is not None:
            command.extend(["--project", project])
        if workspace is not None:
            command.extend(["--workspace", workspace])

        page = run_json(command)
        if not isinstance(page, list):
            raise RuntimeError("twicc sessions returned an unexpected payload")
        if not page:
            return sessions

        sessions.extend(page)
        offset += len(page)


def git_output(args: list[str], *, cwd: Path | None = None) -> str | None:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def git_root(path: str) -> Path | None:
    resolved = Path(path).expanduser()
    if not resolved.exists():
        return None
    output = git_output(["-C", str(resolved), "rev-parse", "--show-toplevel"])
    if output is None:
        return None
    return Path(output).resolve()


def remote_url(path: Path) -> str | None:
    origin = git_output(["-C", str(path), "remote", "get-url", "origin"])
    if origin is not None:
        return origin

    remotes = git_output(["-C", str(path), "remote"])
    if remotes is None:
        return None
    first = next((line.strip() for line in remotes.splitlines() if line.strip()), None)
    if first is None:
        return None
    return git_output(["-C", str(path), "remote", "get-url", first])


def candidate_repo_path(session: dict[str, Any]) -> str | None:
    for key in ("git_directory", "cwd"):
        value = session.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def collect_repos(sessions: list[dict[str, Any]], *, cutoff: datetime) -> list[RepoActivity]:
    repos: dict[Path, RepoActivity] = {}

    for session in sessions:
        active_at = session_activity_at(session)
        if active_at is None or active_at < cutoff:
            continue

        candidate = candidate_repo_path(session)
        if candidate is None:
            continue

        root = git_root(candidate)
        if root is None:
            continue

        repo = repos.get(root)
        if repo is None:
            repo = RepoActivity(local_path=root, remote_url=remote_url(root))
            repos[root] = repo

        repo.sessions += 1
        if isinstance(session.get("project_id"), str):
            repo.projects.add(session["project_id"])
        if isinstance(session.get("git_branch"), str) and session["git_branch"]:
            repo.branches.add(session["git_branch"])
        if repo.latest_activity is None or active_at > repo.latest_activity:
            repo.latest_activity = active_at

    return sorted(
        repos.values(),
        key=lambda repo: repo.latest_activity or datetime.min.replace(tzinfo=UTC),
        reverse=True,
    )


def repo_payload(repos: list[RepoActivity]) -> list[dict[str, Any]]:
    return [
        {
            "local_path": str(repo.local_path),
            "remote_url": repo.remote_url,
            "sessions": repo.sessions,
            "projects": sorted(repo.projects),
            "branches": sorted(repo.branches),
            "latest_activity": repo.latest_activity.isoformat() if repo.latest_activity else None,
        }
        for repo in repos
    ]


def compile_patterns(values: list[str], *, option_name: str) -> list[re.Pattern[str]]:
    patterns: list[re.Pattern[str]] = []
    for value in values:
        try:
            patterns.append(re.compile(value))
        except re.error as exc:
            raise RuntimeError(f"invalid {option_name} regex {value!r}: {exc}") from exc
    return patterns


def repo_filter_text(repo: RepoActivity) -> str:
    values = [
        repo.local_path.name,
        str(repo.local_path),
        repo.remote_url or "",
        *sorted(repo.projects),
        *sorted(repo.branches),
    ]
    return "\n".join(value for value in values if value)


def matches_any(patterns: list[re.Pattern[str]], text: str) -> bool:
    return any(pattern.search(text) for pattern in patterns)


def public_probe_url(remote: str) -> str | None:
    if remote.startswith("file://") or remote.startswith("/") or remote.startswith("."):
        return None

    scp_like = re.match(r"^(?:[^@/:]+@)?(?P<host>[^:/]+):(?P<path>[^\\]+)$", remote)
    if "://" not in remote and scp_like is not None:
        return f"https://{scp_like.group('host')}/{scp_like.group('path')}"

    parsed = urlsplit(remote)
    if parsed.scheme in {"http", "https"}:
        netloc = parsed.hostname or ""
        if parsed.port is not None:
            netloc = f"{netloc}:{parsed.port}"
        return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, ""))

    if parsed.scheme == "ssh" and parsed.hostname and parsed.path:
        return f"https://{parsed.hostname}/{parsed.path.lstrip('/')}"

    return remote


def is_public_repo(repo: RepoActivity, *, timeout: int) -> bool:
    if repo.remote_url is None:
        return False

    probe_url = public_probe_url(repo.remote_url)
    if probe_url is None:
        return False

    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = ""
    env["SSH_ASKPASS"] = ""

    try:
        result = subprocess.run(
            ["git", "ls-remote", "--heads", probe_url],
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False

    return result.returncode == 0


def filter_repos(
    repos: list[RepoActivity],
    *,
    whitelist: list[str],
    blacklist: list[str],
    public_only: bool,
    public_timeout: int,
) -> list[RepoActivity]:
    whitelist_patterns = compile_patterns(whitelist, option_name="--whitelist")
    blacklist_patterns = compile_patterns(blacklist, option_name="--blacklist")
    filtered: list[RepoActivity] = []

    for repo in repos:
        filter_text = repo_filter_text(repo)
        if whitelist_patterns and not matches_any(whitelist_patterns, filter_text):
            continue
        if blacklist_patterns and matches_any(blacklist_patterns, filter_text):
            continue
        if public_only and not is_public_repo(repo, timeout=public_timeout):
            print(f"skipping {repo.local_path}: remote is not publicly readable", file=sys.stderr)
            continue
        filtered.append(repo)

    return filtered


def truncate_text(text: str, *, max_chars: int = MAX_TEXT_CHARS) -> str:
    if len(text) <= max_chars:
        return text
    omitted = len(text) - max_chars
    return text[:max_chars] + f"\n\n[truncated: {omitted} characters omitted]\n"


def slugify_repo(path: Path) -> str:
    stem = path.name or "repo"
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", stem).strip("-")
    return slug or "repo"


def unique_output_path(output_dir: Path, slug: str, used: set[str]) -> Path:
    candidate = slug
    index = 2
    while candidate in used:
        candidate = f"{slug}-{index}"
        index += 1
    used.add(candidate)
    return output_dir / f"{candidate}.md"


def author_patterns(repo: RepoActivity, explicit_authors: list[str]) -> list[str]:
    if explicit_authors:
        return explicit_authors

    candidates = [
        run_text(["git", "config", "user.email"], cwd=repo.local_path),
        run_text(["git", "config", "user.name"], cwd=repo.local_path),
    ]
    values = [value for value in candidates if value]
    return [re.escape(value) for value in values]


def filtered_commit_hashes(repo: RepoActivity, *, since_arg: str, authors: list[str]) -> list[str]:
    command = ["git", "log", f"--since={since_arg}", "--format=%H%x00%an%x00%ae"]
    output = run_text(command, cwd=repo.local_path)
    patterns = [re.compile(pattern) for pattern in authors]
    commit_hashes: list[str] = []
    for line in output.splitlines():
        parts = line.split("\x00")
        if len(parts) != 3:
            continue
        commit_hash, author_name, author_email = parts
        identity = f"{author_name} <{author_email}>"
        if not patterns or any(pattern.search(identity) for pattern in patterns):
            commit_hashes.append(commit_hash)
    return commit_hashes


def git_context(
    repo: RepoActivity,
    *,
    since: datetime,
    authors: list[str],
    include_patch: bool,
    max_patch_chars: int,
) -> GitContext:
    since_arg = since.isoformat()
    authors_filter = author_patterns(repo, authors)
    commits = filtered_commit_hashes(repo, since_arg=since_arg, authors=authors_filter)
    head = run_text(["git", "rev-parse", "--short", "HEAD"], cwd=repo.local_path)
    current_branch = run_text(["git", "branch", "--show-current"], cwd=repo.local_path)

    log = ""
    committed_stat = ""
    committed_names = ""
    committed_patch = ""
    if commits:
        log = run_text(
            [
                "git",
                "show",
                "--date=iso-strict",
                "--pretty=format:commit %h%nAuthor: %an <%ae>%nDate: %ad%nSubject: %s%nBody:%n%b",
                "--name-status",
                "--find-renames",
                *commits,
            ],
            cwd=repo.local_path,
        )
        committed_stat = run_text(
            ["git", "show", "--stat", "--format=commit %h %s", "--find-renames", *commits],
            cwd=repo.local_path,
        )
        committed_names = run_text(
            ["git", "show", "--name-status", "--format=commit %h %s", "--find-renames", *commits],
            cwd=repo.local_path,
        )
        if include_patch:
            committed_patch = run_text(
                ["git", "show", "--format=commit %h %s", "--find-renames", *commits],
                cwd=repo.local_path,
            )

    status = run_text(["git", "status", "--short"], cwd=repo.local_path)
    uncommitted_stat = run_text(["git", "diff", "--stat"], cwd=repo.local_path)
    staged_stat = run_text(["git", "diff", "--cached", "--stat"], cwd=repo.local_path)
    has_contribution = bool(commits or status or uncommitted_stat or staged_stat)

    sections = [
        f"Repository: {repo.local_path}",
        f"Remote: {repo.remote_url or '(none)'}",
        f"HEAD: {head or '(unknown)'}",
        f"Current branch: {current_branch or '(unknown)'}",
        f"TwiCC branches seen: {', '.join(sorted(repo.branches)) or '(none)'}",
        f"TwiCC sessions: {repo.sessions}",
        f"TwiCC latest activity: {repo.latest_activity.isoformat() if repo.latest_activity else '(unknown)'}",
        f"Window starts at: {since_arg}",
        f"Commit author filters: {', '.join(authors_filter) or '(none)'}",
        f"Matching commits: {len(commits)}",
        "",
        "## Matching commits in window",
        log or "(none)",
        "",
        "## Matching committed change summary",
        committed_stat or "(none)",
        "",
        "## Matching committed changed files",
        committed_names or "(none)",
        "",
        "## Working tree status",
        status or "(clean)",
        "",
        "## Uncommitted diff stat",
        uncommitted_stat or "(none)",
        "",
        "## Staged diff stat",
        staged_stat or "(none)",
    ]

    if include_patch:
        sections.extend(
            [
                "",
                "## Matching committed patches",
                truncate_text(committed_patch or "(none)", max_chars=max_patch_chars),
            ]
        )

    return GitContext(text="\n".join(sections), has_contribution=has_contribution)


def build_prompt(repo: RepoActivity, context: str, *, window_label: str, output_file: Path) -> str:
    return f"""You are writing a concise user-facing project changelog.

Write the changelog in English as Markdown.

Audience:
- A developer reviewing what moved forward recently.
- Keep it product/project oriented, not a raw Git summary.

Focus:
- Features delivered or advanced.
- Refactors and architecture cleanup.
- Bug fixes and reliability improvements.
- Notable tests or validation only when they clarify confidence.

Avoid:
- Overly technical implementation detail.
- Listing every touched file.
- Mentioning hashes unless a commit is essential context.
- Inventing work that is not supported by the Git context.

If there is no meaningful change in the provided window, say that briefly.

Metadata:
- Repository: {repo.local_path}
- Remote: {repo.remote_url or "(none)"}
- Window: {window_label}
- Output file: {output_file.name}

Git context:

```text
{context}
```
"""


def run_codex(codex: list[str], *, output_dir: Path, output_file: Path, input_file: Path) -> None:
    relative_input = input_file.relative_to(output_dir)
    prompt = (
        f"Read {relative_input.as_posix()} and produce exactly the requested changelog. "
        "Return only the final Markdown changelog."
    )
    command = [
        *codex,
        "exec",
        "--ephemeral",
        "--color",
        "never",
        "--skip-git-repo-check",
        "--output-last-message",
        str(output_file),
        prompt,
    ]
    result = subprocess.run(
        command,
        cwd=output_dir,
        stdin=subprocess.DEVNULL,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit code {result.returncode}"
        raise RuntimeError(f"codex failed for {output_file.name}: {detail}")


def run_claude(
    claude: list[str],
    *,
    output_dir: Path,
    output_file: Path,
    input_file: Path,
    model: str | None,
) -> None:
    command = [
        *claude,
        "-p",
        "--output-format",
        "text",
        "--no-session-persistence",
        "--tools",
        "",
    ]
    if model is not None:
        command.extend(["--model", model])

    result = subprocess.run(
        command,
        cwd=output_dir,
        input=input_file.read_text(),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit code {result.returncode}"
        raise RuntimeError(f"claude failed for {output_file.name}: {detail}")

    output_file.write_text(result.stdout)


def run_generator(
    generator: str,
    *,
    codex: list[str],
    claude: list[str],
    claude_model: str | None,
    output_dir: Path,
    output_file: Path,
    input_file: Path,
) -> None:
    if generator == "codex":
        run_codex(codex, output_dir=output_dir, output_file=output_file, input_file=input_file)
        return
    if generator == "claude":
        run_claude(
            claude,
            output_dir=output_dir,
            output_file=output_file,
            input_file=input_file,
            model=claude_model,
        )
        return
    raise RuntimeError(f"unsupported generator: {generator}")


def write_index(output_dir: Path, generated: list[tuple[RepoActivity, Path]]) -> None:
    lines = ["# Project Changelogs", ""]
    for repo, path in generated:
        rel = path.relative_to(output_dir)
        lines.append(f"- [{repo.local_path.name}]({rel.as_posix()}) - `{repo.local_path}`")
    lines.append("")
    (output_dir / "index.md").write_text("\n".join(lines))


def clean_output_dir(output_dir: Path) -> None:
    if not output_dir.exists():
        return
    if not output_dir.is_dir():
        raise RuntimeError(f"output path is not a directory: {output_dir}")

    for markdown_file in output_dir.glob("*.md"):
        if markdown_file.is_file() or markdown_file.is_symlink():
            markdown_file.unlink()

    input_dir = output_dir / "_inputs"
    if input_dir.exists():
        if not input_dir.is_dir() or input_dir.is_symlink():
            raise RuntimeError(f"refusing to clean non-directory input path: {input_dir}")
        shutil.rmtree(input_dir)


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate project changelogs directly from recent TwiCC session activity.",
    )
    parser.add_argument("window", type=parse_window, help="Activity window, for example 24h or 7d.")
    parser.add_argument("output_dir", help="Directory where changelog Markdown files will be written.")
    parser.add_argument("--page-size", type=positive_int, default=1000, help="TwiCC sessions page size. Default: 1000.")
    parser.add_argument("--include-hidden", action="store_true", help="Include hidden TwiCC sessions.")
    parser.add_argument("--include-archived", action="store_true", help="Include archived TwiCC sessions.")
    parser.add_argument("--project", help="Limit to a TwiCC project path or id.")
    parser.add_argument("--workspace", help="Limit to a TwiCC workspace id.")
    parser.add_argument(
        "--whitelist",
        "--allowlist",
        action="append",
        default=[],
        help="Only include repositories matching this regex. Repeatable. Matches repo name, path, remote, TwiCC projects, and branches.",
    )
    parser.add_argument(
        "--blacklist",
        "--denylist",
        action="append",
        default=[],
        help="Exclude repositories matching this regex. Repeatable. Matches repo name, path, remote, TwiCC projects, and branches.",
    )
    parser.add_argument(
        "--public",
        action="store_true",
        help="Only include repositories whose remote is publicly readable without credentials.",
    )
    parser.add_argument("--public-timeout", type=positive_int, default=10, help="Seconds to wait per public remote check.")
    parser.add_argument("--twicc-bin", help="TwiCC executable. Defaults to TWICC_BIN or 'twicc'.")
    parser.add_argument(
        "--author",
        action="append",
        default=[],
        help=(
            "Git author regex to include. Repeatable. Defaults to each repo's "
            "git config user.email and user.name."
        ),
    )
    parser.add_argument(
        "--generator",
        choices=("codex", "claude"),
        default="codex",
        help="LLM CLI used to write changelogs. Default: codex.",
    )
    parser.add_argument("--codex-bin", help="Codex executable. Defaults to CODEX_BIN or 'codex'.")
    parser.add_argument("--claude-bin", help="Claude executable. Defaults to CLAUDE_BIN or 'claude'.")
    parser.add_argument("--claude-model", help="Optional Claude model alias or full model name.")
    parser.add_argument("--include-patch", action="store_true", help="Include committed patch text in the agent context.")
    parser.add_argument(
        "--include-empty",
        action="store_true",
        help="Generate changelogs even when no matching commits or local changes are found.",
    )
    parser.add_argument("--max-patch-chars", type=positive_int, default=60_000, help="Max committed patch characters per repo.")
    parser.add_argument("--dry-run", action="store_true", help="Write agent input files, but do not run the generator.")
    parser.add_argument("--repos-json", help="Optional path where the discovered active repositories JSON is written.")
    parser.add_argument(
        "--clean",
        action="store_true",
        help="Remove previous generated Markdown files and _inputs/ from output_dir before running.",
    )
    args = parser.parse_args()

    if args.project and args.workspace:
        parser.error("--project and --workspace are mutually exclusive")

    output_dir = Path(args.output_dir).expanduser().resolve()
    input_dir = output_dir / "_inputs"
    since = datetime.now(UTC) - args.window
    window_label = f"since {since.isoformat()}"

    try:
        twicc = resolve_executable(args.twicc_bin, env_name="TWICC_BIN", default="twicc")
        codex = []
        claude = []
        if not args.dry_run and args.generator == "codex":
            codex = resolve_executable(args.codex_bin, env_name="CODEX_BIN", default="codex")
        if not args.dry_run and args.generator == "claude":
            claude = resolve_executable(args.claude_bin, env_name="CLAUDE_BIN", default="claude")
        sessions = load_sessions(
            twicc=twicc,
            page_size=args.page_size,
            include_hidden=args.include_hidden,
            include_archived=args.include_archived,
            project=args.project,
            workspace=args.workspace,
        )
        repos = filter_repos(
            collect_repos(sessions, cutoff=since),
            whitelist=args.whitelist,
            blacklist=args.blacklist,
            public_only=args.public,
            public_timeout=args.public_timeout,
        )
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    try:
        if args.clean:
            clean_output_dir(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        input_dir.mkdir(exist_ok=True)

        if args.repos_json:
            Path(args.repos_json).expanduser().resolve().write_text(json.dumps(repo_payload(repos), indent=2) + "\n")
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    generated: list[tuple[RepoActivity, Path]] = []
    used_slugs: set[str] = set()

    for repo in repos:
        if not repo.local_path.exists():
            print(f"warning: skipping missing repo: {repo.local_path}", file=sys.stderr)
            continue

        output_file = unique_output_path(output_dir, slugify_repo(repo.local_path), used_slugs)
        input_file = input_dir / f"{output_file.stem}.txt"

        try:
            context = git_context(
                repo,
                since=since,
                authors=args.author,
                include_patch=args.include_patch,
                max_patch_chars=args.max_patch_chars,
            )
            if not context.has_contribution and not args.include_empty:
                print(f"skipping {repo.local_path}: no matching commits or local changes", file=sys.stderr)
                continue

            prompt = build_prompt(repo, context.text, window_label=window_label, output_file=output_file)
            input_file.write_text(prompt)

            if args.dry_run:
                output_file.write_text(
                    f"# {repo.local_path.name}\n\nDry run only. Agent input written to `{input_file}`.\n"
                )
            else:
                run_generator(
                    args.generator,
                    codex=codex,
                    claude=claude,
                    claude_model=args.claude_model,
                    output_dir=output_dir,
                    output_file=output_file,
                    input_file=input_file,
                )
        except RuntimeError as exc:
            print(f"warning: {exc}", file=sys.stderr)
            continue

        generated.append((repo, output_file))
        print(output_file)

    write_index(output_dir, generated)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

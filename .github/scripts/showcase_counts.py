#!/usr/bin/env python3
"""repo-showcase: live counters instead of hard-coded numbers (STANDALONE: standard library only).

This very file is copied into a repository by `install-action` (.github/scripts/showcase_counts.py) so that a GitHub
Action keeps the numbers of the README and the SVG visuals up to date. It must never import another module.

Usage: counts.py {sync|lint|badge|install-action} <repo> [options]
  sync <repo> [--check]        recompute the counters of .showcase.json and rewrite the markers
                               (--check writes nothing; exit 4 if a file is stale or a marker is unknown)
  lint <repo>                  find numbers that look like counts or measures in the README, the root .md (not
                               CHANGELOG, HISTORY, RELEASE_NOTES, NEWS: dated by nature), assets/**/*.svg and every
                               local SVG the README displays; "files_scanned" lists what was linted
  badge <repo> --kind K        shields.io URL and Markdown for stars|forks|release|last-commit|license|downloads|files
        [--path DIR] [--label TEXT] [--color HEX] [--slug OWNER/REPO]
  install-action <repo> [--force] [--branch NAME]
                               copy this file and a workflow into the repository (nothing is committed);
                               needs .showcase.json: the workflow does a blobless sparse checkout of the files it uses

.showcase.json (at the repository root):
  {"counters": {"vst": {"glob": "plugins/*.md", "exclude": ["README.md"]},       files matching the glob(s)
                "packs": {"dirs": "packs/*"},                                   folders matching the glob
                "ideas": {"lines": "data/ideas.txt"},                           non-empty lines not starting with #
                "items": {"json": "data/items.json", "path": "items"}},         length of the array at a dotted path
   "files": ["README.md", "assets/*.svg"],            files whose markers are rewritten (default: root *.md, assets SVG)
   "format": {"vst": {"round": 10, "suffix": "+", "sep": ","}}}   optional: round down to a multiple, add a
                                                                suffix, separate thousands ("3,350+")
glob and dirs count the files tracked by git (git ls-files, so a sparse or blobless checkout is enough and untracked
files are ignored) when the repository is a git work tree, else the files found on disk. In patterns, * and ? stay
inside a folder, **/ matches zero or more folders, [...] is a character class. lines and json read the file on disk,
else from HEAD.
Markers: Markdown or HTML  <!-- count:vst -->57<!-- /count -->   SVG or HTML  <tspan data-count="vst">57</tspan>
A line carrying "showcase: ignore" is skipped by lint.

Output: one JSON object on stdout. Exit codes: 0 ok; 1 unexpected error; 2 usage or invalid configuration;
4 stale counters, unknown marker or count-like numbers; 6 safety refusal (missing configuration, file exists).
Python 3.9 compatible.
"""
import argparse
import fnmatch
import html
import json
import re
import shutil
import subprocess
import sys
import traceback
from pathlib import Path
from urllib.parse import quote, unquote

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_FINDINGS = 4
EXIT_REFUSED = 6

CONFIG_NAME = ".showcase.json"
DEFAULT_FILES = ["*.md", "assets/**/*.svg"]
SKIP_PARTS = {".git", "node_modules", ".venv", "__pycache__"}
IGNORE_MARKERS = ("showcase: ignore", "check-repo: ignore")
NAME_RE = r"[A-Za-z0-9_.-]+"
MARKER_RE = re.compile(r"(<!--\s*count:(%s)\s*-->)(.*?)(<!--\s*/count\s*-->)" % NAME_RE)
DATA_RE = re.compile(r"(<([A-Za-z][\w:.-]*)\b[^<>]*?\sdata-count\s*=\s*[\"'](%s)[\"'][^<>]*>)([^<]*)(</\2\s*>)" % NAME_RE)
GITHUB_REMOTE_RE = re.compile(r"github\.com[:/]+([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$")


class CountsError(Exception):
    """Expected failure: reported as {"ok": false, "error": message} with the given exit code."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def print_json(data):
    json.dump(data, sys.stdout, ensure_ascii=False, indent=1, default=str)
    sys.stdout.write("\n")


def _root(path):
    root = Path(path).expanduser().resolve()
    if not root.is_dir():
        raise CountsError(EXIT_USAGE, "not a directory: %s" % root)
    return root


def _git_raw(root, *args):
    """Standard output of a git command run in root, as bytes; None when git is missing or the command fails."""
    try:
        result = subprocess.run(["git", "-C", str(root)] + list(args), capture_output=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 else None


def _as_list(value):
    if value is None:
        return []
    return [value] if isinstance(value, str) else list(value)


# --------------------------------------------------------------------------- configuration and counters

def load_config(root):
    path = Path(root) / CONFIG_NAME
    if not path.is_file():
        raise CountsError(EXIT_REFUSED, "%s not found at the repository root: create it first" % CONFIG_NAME)
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise CountsError(EXIT_USAGE, "%s is not valid JSON: %s" % (CONFIG_NAME, error))
    counters = config.get("counters") if isinstance(config, dict) else None
    if not isinstance(counters, dict) or not counters:
        raise CountsError(EXIT_USAGE, '%s needs a non-empty "counters" object' % CONFIG_NAME)
    for name in counters:
        if not re.fullmatch(NAME_RE, name):
            raise CountsError(EXIT_USAGE, "invalid counter name: %r" % name)
    return config


def _check_pattern(pattern):
    if not isinstance(pattern, str) or not pattern or pattern.startswith("/") or ".." in Path(pattern).parts:
        raise CountsError(EXIT_USAGE, "invalid glob (must be relative, without ..): %r" % (pattern,))


def _glob(root, pattern, dirs):
    """Relative POSIX paths of the files (or folders) matching a glob under root, walking the filesystem."""
    _check_pattern(pattern)
    found = set()
    try:
        for path in Path(root).glob(pattern):
            rel = path.relative_to(root)
            if set(rel.parts) & SKIP_PARTS:
                continue
            if (path.is_dir() if dirs else path.is_file()):
                found.add(rel.as_posix())
    except (ValueError, NotImplementedError) as error:
        raise CountsError(EXIT_USAGE, "invalid glob %r: %s" % (pattern, error))
    return found


def _segment_regex(segment):
    """Regular expression for one path segment: * and ? never cross a slash, [...] is a class ([!...] negates)."""
    out, i, size = [], 0, len(segment)
    while i < size:
        char = segment[i]
        if char == "*":
            while i + 1 < size and segment[i + 1] == "*":
                i += 1
            out.append("[^/]*")
        elif char == "?":
            out.append("[^/]")
        elif char == "[":
            j = i + 1
            if j < size and segment[j] in "!^":
                j += 1
            if j < size and segment[j] == "]":
                j += 1
            while j < size and segment[j] != "]":
                j += 1
            if j >= size:
                out.append(re.escape(char))
            else:
                body = segment[i + 1:j]
                negated = body[:1] in ("!", "^")
                body = re.sub(r"([\\\[\]^])", r"\\\1", body[1:] if negated else body)
                out.append("[%s%s]" % ("^/" if negated else "", body))
                i = j
        else:
            out.append(re.escape(char))
        i += 1
    return "".join(out)


def glob_regex(pattern):
    """Compiled regular expression for a glob over POSIX relative paths: * and ? stay inside a folder, a **/
    segment matches zero or more folders, a trailing ** matches everything below, [...] is a character class."""
    _check_pattern(pattern)
    parts = [part for part in pattern.split("/") if part and part != "."]
    if not parts:
        raise CountsError(EXIT_USAGE, "invalid glob: %r" % (pattern,))
    regex = ""
    for index, part in enumerate(parts):
        last = index == len(parts) - 1
        if part == "**":
            regex += ".+" if last else "(?:[^/]+/)*"
        else:
            regex += _segment_regex(part) + ("" if last else "/")
    return re.compile(regex)


class Tracked:
    """The paths tracked by git under a repository root (what `git ls-files` lists, skip-worktree entries too)."""

    def __init__(self, files):
        self.files = sorted(files)
        self._folders = None

    @property
    def folders(self):
        """Distinct ancestor folders of the tracked files."""
        if self._folders is None:
            folders = set()
            for rel in self.files:
                parts = rel.split("/")[:-1]
                for depth in range(1, len(parts) + 1):
                    folders.add("/".join(parts[:depth]))
            self._folders = folders
        return self._folders


def tracked_paths(root):
    """Tracked paths of a git work tree, or None (not a repository, git missing, nothing tracked yet): the caller
    then walks the filesystem."""
    if (_git_raw(root, "rev-parse", "--is-inside-work-tree") or b"").strip() != b"true":
        return None
    listing = _git_raw(root, "ls-files", "-z") or b""
    files = [name for name in listing.decode("utf-8", "replace").split("\0") if name]
    return Tracked(files) if files else None


def _glob_tracked(tracked, pattern, dirs):
    regex = glob_regex(pattern)
    return {rel for rel in (tracked.folders if dirs else tracked.files)
            if regex.fullmatch(rel) and not set(rel.split("/")) & SKIP_PARTS}


def _excluded(root, rel, patterns):
    for pattern in patterns:
        if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(rel.rsplit("/", 1)[-1], pattern):
            return True
    return False


def _dotted(data, path):
    for part in [p for p in path.split(".") if p]:
        if isinstance(data, list) and part.isdigit() and int(part) < len(data):
            data = data[int(part)]
        elif isinstance(data, dict) and part in data:
            data = data[part]
        else:
            raise CountsError(EXIT_USAGE, "path %r not found in the JSON file" % path)
    return data


_AUTO = object()


def _read_counted(root, name, relative):
    """Text of a file counted by a lines or json counter: from disk, else from HEAD (sparse checkout)."""
    target = Path(root) / str(relative)
    if target.is_file():
        return target.read_text(encoding="utf-8", errors="replace")
    if isinstance(relative, str) and relative and not relative.startswith("/") and ".." not in Path(relative).parts:
        shown = _git_raw(root, "show", "HEAD:./%s" % relative)
        if shown is not None:
            return shown.decode("utf-8", errors="replace")
    raise CountsError(EXIT_USAGE, "counter %s: file not found: %s" % (name, relative))


def evaluate(root, name, spec, tracked=_AUTO):
    """Value of one counter. glob and dirs count the paths tracked by git when root is a git work tree (pass
    tracked=None to force the filesystem walk), else the files found on disk."""
    root = Path(root)
    if not isinstance(spec, dict):
        raise CountsError(EXIT_USAGE, "counter %s: expected an object" % name)
    exclude = _as_list(spec.get("exclude"))
    for key, dirs in (("glob", False), ("dirs", True)):
        if key in spec:
            if tracked is _AUTO:
                tracked = tracked_paths(root)
            found = set()
            for pattern in _as_list(spec[key]):
                found |= _glob_tracked(tracked, pattern, dirs) if tracked else _glob(root, pattern, dirs)
            return len([rel for rel in found if not _excluded(root, rel, exclude)])
    if "lines" in spec or "json" in spec:
        key = "lines" if "lines" in spec else "json"
        text = _read_counted(root, name, spec[key])
        if key == "lines":
            return sum(1 for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#"))
        try:
            value = _dotted(json.loads(text), str(spec.get("path", "")))
        except ValueError as error:
            raise CountsError(EXIT_USAGE, "counter %s: %s is not valid JSON: %s" % (name, spec[key], error))
        if not isinstance(value, (list, dict)):
            raise CountsError(EXIT_USAGE, "counter %s: the value at the path is not an array" % name)
        return len(value)
    raise CountsError(EXIT_USAGE, "counter %s: needs one of glob, dirs, lines, json" % name)


def display_value(config, name, value):
    """Text written in the markers: the value rounded down to a multiple and suffixed, if configured."""
    fmt = (config.get("format") or {}).get(name) or {}
    step = fmt.get("round")
    shown = value
    suffix = fmt.get("suffix", "")
    if isinstance(step, int) and step > 1:
        shown = value // step * step
        if shown == 0 and value > 0:
            shown, suffix = value, ""
    number = str(shown)
    sep = fmt.get("sep")
    if isinstance(sep, str) and sep:
        number = "{:,}".format(shown).replace(",", sep)
    return "%s%s" % (number, suffix)


def rewrite(text, displays):
    """(new text, names of unknown markers, number of markers) after updating every known marker."""
    unknown, seen = [], [0]

    def marker(match):
        seen[0] += 1
        name = match.group(2)
        if name not in displays:
            unknown.append(name)
            return match.group(0)
        return match.group(1) + displays[name] + match.group(4)

    def element(match):
        seen[0] += 1
        name = match.group(3)
        if name not in displays:
            unknown.append(name)
            return match.group(0)
        return match.group(1) + displays[name] + match.group(5)

    return DATA_RE.sub(element, MARKER_RE.sub(marker, text)), unknown, seen[0]


def _target_files(root, config):
    found = set()
    for pattern in _as_list(config.get("files")) or DEFAULT_FILES:
        found |= _glob(root, pattern, False)
    return sorted(found)


def sync(root, check=False):
    """Recompute every counter and update the markers; with check=True nothing is written."""
    root = Path(root)
    config = load_config(root)
    tracked = tracked_paths(root)
    values = {name: evaluate(root, name, spec, tracked) for name, spec in config["counters"].items()}
    displays = {name: display_value(config, name, value) for name, value in values.items()}
    updated, stale, unknown, markers = [], [], [], 0
    for rel in _target_files(root, config):
        path = root / rel
        try:
            old = path.read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        new, bad, seen = rewrite(old, displays)
        markers += seen
        unknown.extend({"file": rel, "name": name} for name in bad)
        if new != old:
            if check:
                stale.append(rel)
            else:
                path.write_bytes(new.encode("utf-8"))
                updated.append(rel)
    return {"counters": values, "display": displays, "updated": updated, "stale": stale, "unknown": unknown,
            "markers": markers}


# --------------------------------------------------------------------------- lint

MONTHS = ("jan feb mar apr may jun jul aug sep sept oct nov dec january february march april june july august "
          "september october november december janvier fevrier mars avril mai juin juillet aout septembre octobre "
          "novembre decembre").split()
LABELS = set(("step phase part chapter section level tier figure fig table page day week issue pr option rule tip no "
              "number top version v release python node nodejs java go ios android macos windows ubuntu php ruby "
              "rust react vue angular next django flask gpt claude gemini llama http ipv").split())
UNIT_ATTACHED = set("mb kb gb tb ms s fps px x hz khz mhz ghz kg km cm mm".split())
ORDINALS = set("st nd rd th er ere eme e nde".split())
MULTIPLIERS = {"k", "m"}
TIME_WORDS = r"(?:min|mins|sec|secs|seconds?|minutes?|hours?|hrs?|days?|weeks?|months?|years?|bytes?|bits?|tokens?)"
UNIT_SPACED = re.compile(r"^\s{1,2}(%%|×|(?:[kmgt]i?b|ms|s|fps|px|x|hz|[kmg]hz|%s)(?![A-Za-z]))" % TIME_WORDS, re.I)
EDITORIAL = set("steps ways clicks things reasons tips tricks".split())
NOT_NOUNS = set("is as was has this his its yes plus thus less does always perhaps versus across status bus "
                "ours yours theirs whereas".split())
BADGE_KIND = {"stars": "stars", "star": "stars", "forks": "forks", "downloads": "downloads", "releases": "release",
              "versions": "release", "commits": "last-commit"}
NUM_RE = re.compile(r"(?<![\w.,/#$€£:-])(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)*)(?!\d)")
WORD_RE = re.compile(r"\s+([^\W\d_][\w'’-]*)(?:\s+([^\W\d_][\w'’-]*))?")


def _fold(text):
    import unicodedata
    return "".join(ch for ch in unicodedata.normalize("NFKD", text) if not unicodedata.combining(ch)).lower()


def visible_markdown(text):
    """Yield (line number, visible text): outside code, comments, markers, URLs, images and HTML tags."""
    fence, in_comment = None, False
    for number, raw in enumerate(text.splitlines(), 1):
        stripped = raw.strip()
        if stripped.startswith(("```", "~~~")):
            fence = None if fence == stripped[:3] else (fence or stripped[:3])
            continue
        if fence:
            continue
        line = DATA_RE.sub(" ", MARKER_RE.sub(" ", raw))
        kept = []
        while line:
            if in_comment:
                end = line.find("-->")
                if end < 0:
                    break
                line, in_comment = line[end + 3:], False
                continue
            start = line.find("<!--")
            if start < 0:
                kept.append(line)
                break
            kept.append(line[:start] + " ")
            end = line.find("-->", start + 4)
            if end < 0:
                in_comment = True
                break
            line = line[end + 3:]
        line = "".join(kept)
        if re.match(r"\s*\[[^\]]+\]:\s*\S+", line):
            continue
        line = re.sub(r"`[^`]*`", " ", line)
        line = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", line)
        line = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", line)
        line = re.sub(r"\[([^\]]*)\]\[[^\]]*\]", r"\1", line)
        line = re.sub(r"<[^>]*>", " ", line)
        line = re.sub(r"(?:https?://|www\.)\S+", " ", line)
        line = re.sub(r"\*\*|__|\*|~~", "", line)
        line = re.sub(r"^\s*(?:>\s*)*(?:#+\s+|[-*+]\s+|\d+[.)]\s+)?", "", html.unescape(line))
        if line.strip():
            yield number, line


def visible_svg(text):
    """Yield (line number, visible text) of the <text> elements of an SVG, markers excluded."""
    cleaned = DATA_RE.sub(" ", MARKER_RE.sub(" ", text))
    for match in re.finditer(r"<text\b[^>]*>(.*?)</text\s*>", cleaned, re.S):
        inner = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", match.group(1)))).strip()
        if inner:
            yield cleaned.count("\n", 0, match.start()) + 1, inner


def _plural_noun(word):
    w = word.lower().strip("'’")
    return len(w) >= 3 and w.endswith("s") and not w.endswith(("ss", "us", "is", "ous")) and w not in NOT_NOUNS


def _folder_index(root):
    """{singular folder name: relative path} for the folders at most two levels deep."""
    index = {}
    for first in sorted(Path(root).iterdir()):
        if not first.is_dir() or first.name.startswith(".") or first.name in SKIP_PARTS:
            continue
        index.setdefault(_singular(first.name), first.name)
        for second in sorted(first.iterdir()):
            if second.is_dir() and not second.name.startswith("."):
                index.setdefault(_singular(second.name), "%s/%s" % (first.name, second.name))
    return index


def _singular(word):
    w = word.lower()
    if w.endswith("ies") and len(w) > 4:
        return w[:-3] + "y"
    return w[:-1] if w.endswith("s") and not w.endswith("ss") else w


def _suggest(root, index, noun, number, is_svg):
    """(suggestion text, counter proposal or None) for a count-like number."""
    folder = index.get(_singular(noun))
    if folder:
        base = Path(root) / folder
        children = [c for c in base.iterdir() if not c.name.startswith(".")]
        dirs = sum(1 for c in children if c.is_dir())
        name = re.sub(r"[^A-Za-z0-9_.-]+", "_", folder.rsplit("/", 1)[-1].lower())
        spec = {"dirs": "%s/*" % folder} if dirs and dirs >= len(children) - dirs else {"glob": "%s/*" % folder}
        if "glob" in spec and (base / "README.md").is_file():
            spec["exclude"] = ["README.md"]
        marker = ('<tspan data-count="%s">%s</tspan>' if is_svg else "<!-- count:%s -->%s<!-- /count -->") % (
            name, number)
        text = ('A %s/ folder exists: add the counter to %s ({"counters": {"%s": %s}}) and wrap the number as %s; '
                "then run counts sync (counts install-action keeps it updated on every push).") % (
                    folder, CONFIG_NAME, name, json.dumps(spec), marker)
        return text, {"name": name, "config": spec, "marker": marker}
    kind = BADGE_KIND.get(noun.lower())
    if kind:
        return "Use a live badge instead: counts badge --kind %s." % kind, None
    return ("Reword without a number (for example 'dozens of', 'a growing collection'), or define a counter in %s "
            "if the thing can be counted from files, folders, lines or a JSON array." % CONFIG_NAME), None


def scan_line(text):
    """[(kind, token, noun, start, end)] for the numbers of one visible line (kind: count-like or measure)."""
    hits = []
    for match in NUM_RE.finditer(text):
        token, rest = match.group(1), text[match.end():]
        before = text[:match.start()]
        dotted = "." in token
        if not dotted and len(token) == 4 and 1900 <= int(token) <= 2099:
            continue
        if re.match(r"[-–/:]\d", rest) or (match.start() <= 3 and re.match(r"[.)]\s", rest)):
            continue
        prev = re.search(r"([^\W\d_]+)[\s:.]*$", before)
        prev_word = _fold(prev.group(1)) if prev else ""
        nxt = re.match(r"\s*([^\W\d_]+)", rest)
        if prev_word in LABELS or prev_word in MONTHS or (nxt and _fold(nxt.group(1)) in MONTHS):
            continue
        if re.match(r"\s*(?:[€$£]|usd\b|eur\b)", rest, re.I):
            continue
        attached = re.match(r"[^\W\d_]+", rest)
        letters = attached.group(0).lower() if attached else ""
        if rest.startswith(("%", "×")) or UNIT_SPACED.match(rest) or letters in UNIT_ATTACHED:
            hits.append(("measure", token, None, match.start(), match.end()))
            continue
        if letters in ORDINALS or (letters and letters not in MULTIPLIERS):
            continue
        if dotted:
            continue
        tail = rest[len(letters):].lstrip("+") if letters else rest.lstrip("+")
        words = WORD_RE.match(tail)
        noun = None
        if words:
            for word in words.groups():
                if word and word.lower() not in EDITORIAL and _plural_noun(word):
                    noun = word
                    break
            if noun is None and words.group(1) and words.group(1).lower() in EDITORIAL:
                continue
        if noun:
            hits.append(("count-like", token, noun, match.start(), match.end()))
        elif letters in MULTIPLIERS:
            hits.append(("measure", token + letters, None, match.start(), match.end()))
    return hits


DATED_DOCS = ("changelog*", "history*", "release_notes*", "release-notes*", "news*")
IMAGE_MARKDOWN_RE = re.compile(r"!\[[^\]]*\]\(\s*(<[^>]*>|[^)\s]+)")
IMAGE_REFERENCE_RE = re.compile(r"!\[[^\]]*\]\[([^\]]*)\]")
REFERENCE_DEFINITION_RE = re.compile(r"^\s{0,3}\[([^\]]+)\]:\s*(<[^>]*>|\S+)", re.M)
IMG_SRC_RE = re.compile(r"<img\b[^>]*?\ssrc\s*=\s*[\"']([^\"']+)[\"']", re.I)
SOURCE_SRCSET_RE = re.compile(r"<source\b[^>]*?\ssrcset\s*=\s*[\"']([^\"']+)[\"']", re.I)


def _lint_markdown_files(root):
    """Root Markdown files to lint, the README first; dated documents (changelog, history...) are left out."""
    names = sorted(_glob(root, "*.md", False), key=lambda n: (n.lower() != "readme.md", n))
    kept = [n for n in names if not any(fnmatch.fnmatch(n.lower(), pattern) for pattern in DATED_DOCS)]
    return kept, [n for n in names if n not in kept]


def referenced_svgs(root, readme_text):
    """Local .svg files the README displays (Markdown images, <img src>, <source srcset>) that exist under root."""
    text = "\n".join(_outside_fences(readme_text))
    definitions = {m.group(1).strip().lower(): m.group(2) for m in REFERENCE_DEFINITION_RE.finditer(text)}
    targets = [m.group(1) for m in IMAGE_MARKDOWN_RE.finditer(text)]
    targets += [definitions[m.group(1).strip().lower()] for m in IMAGE_REFERENCE_RE.finditer(text)
                if m.group(1).strip().lower() in definitions]
    targets += [m.group(1) for m in IMG_SRC_RE.finditer(text)]
    for match in SOURCE_SRCSET_RE.finditer(text):
        targets += [candidate.split()[0] for candidate in match.group(1).split(",") if candidate.split()]
    base = Path(root).resolve()
    found = set()
    for target in targets:
        target = unquote(target.strip().strip("<>").split("#", 1)[0].split("?", 1)[0])
        if not target.lower().endswith(".svg") or re.match(r"[A-Za-z][A-Za-z0-9+.-]*:|//", target):
            continue
        path = (base / target.lstrip("/")).resolve()
        try:
            rel = path.relative_to(base)
        except ValueError:
            continue
        if path.is_file() and not set(rel.parts) & SKIP_PARTS:
            found.add(rel.as_posix())
    return found


def _outside_fences(text):
    fence = None
    for raw in text.splitlines():
        stripped = raw.strip()
        if stripped.startswith(("```", "~~~")):
            fence = None if fence == stripped[:3] else (fence or stripped[:3])
        elif not fence:
            yield raw


def lint(root):
    """Numbers that look like hard-coded counts (warnings) or measures (info) in the README and the other root .md
    files (not changelogs), in assets/**/*.svg and in every local SVG the README displays."""
    root = Path(root)
    markdown, dated = _lint_markdown_files(root)
    svgs = _glob(root, "assets/**/*.svg", False)
    readme = next((n for n in markdown if n.lower() == "readme.md"), None)
    if readme:
        try:
            svgs |= referenced_svgs(root, (root / readme).read_bytes().decode("utf-8"))
        except (OSError, UnicodeDecodeError):
            pass
    files = markdown + sorted(svgs)
    index = _folder_index(root)
    items = []
    for rel in files:
        try:
            text = (root / rel).read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        is_svg = rel.endswith(".svg")
        lines = text.splitlines()
        for number, visible in (visible_svg(text) if is_svg else visible_markdown(text)):
            if any(m in (lines[number - 1] if number <= len(lines) else "") for m in IGNORE_MARKERS):
                continue
            for kind, token, noun, start, end in scan_line(visible):
                snippet = re.sub(r"\s+", " ", visible[max(0, start - 40):end + 40]).strip()
                item = {"file": rel, "line": number, "snippet": snippet, "kind": kind, "number": token}
                if kind == "count-like":
                    item["level"] = "warning"
                    item["noun"] = noun
                    item["suggestion"], item["counter"] = _suggest(root, index, noun, token, is_svg)
                else:
                    item["level"] = "info"
                    item["suggestion"] = ("Fine if it is a measurement and the text says when it was taken "
                                          "(for example '75 % in the October 2026 run'); otherwise automate it.")
                items.append(item)
    warnings = sum(1 for item in items if item["level"] == "warning")
    return {"items": items, "warnings": warnings, "info": len(items) - warnings, "files_scanned": files,
            "files_skipped_dated": dated}


# --------------------------------------------------------------------------- badge

def _git(root, *args):
    try:
        result = subprocess.run(["git", "-C", str(root)] + list(args), capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.decode("utf-8", "replace").strip() if result.returncode == 0 else ""


def _slug(root):
    match = GITHUB_REMOTE_RE.search(_git(root, "remote", "get-url", "origin"))
    return "%s/%s" % match.groups() if match else None


def _branch(root):
    ref = _git(root, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")
    if "/" in ref:
        return ref.split("/", 1)[1]
    return _git(root, "symbolic-ref", "--quiet", "--short", "HEAD") or "main"


BADGE_PATHS = {"stars": ("github/stars/%s", "stargazers", "stars"), "forks": ("github/forks/%s", "forks", "forks"),
               "release": ("github/v/release/%s", "releases", "release"),
               "last-commit": ("github/last-commit/%s", "commits", "last commit"),
               "license": ("github/license/%s", "", "license"), "downloads": ("github/downloads/%s/total",
                                                                               "releases", "downloads")}


def make_badge(root, kind, path="", label=None, color=None, slug=None, branch=None):
    slug = slug or _slug(root)
    if not slug or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", slug):
        raise CountsError(EXIT_USAGE, "no GitHub origin found: pass --slug OWNER/REPO")
    if color is not None and not re.fullmatch(r"#?[0-9a-fA-F]{3,8}", color):
        raise CountsError(EXIT_USAGE, "--color must be a hex value such as 0a7cff")
    params = []
    caveats = []
    repo_url = "https://github.com/%s" % slug
    if kind == "files":
        branch = branch or _branch(root)
        folder = path.strip("/")
        api = "https://api.github.com/repos/%s/contents%s?ref=%s" % (slug, "/" + folder if folder else "", branch)
        url = "https://img.shields.io/badge/dynamic/json?url=%s&query=%s" % (quote(api, safe=""), quote("$.length"))
        link = "%s/tree/%s%s" % (repo_url, branch, "/" + folder if folder else "")
        default_label = "files"
        caveats = ["The badge counts the files AND the folders directly inside the folder (query $.length).",
                   "The GitHub contents API returns at most 1,000 entries per folder.",
                   "No filtering is possible: for a filtered count use markers with counts sync and counts "
                   "install-action.",
                   "The repository must be public for the badge to render."]
    else:
        template, suffix, default_label = BADGE_PATHS[kind]
        url = "https://img.shields.io/" + template % slug
        link = "%s/%s" % (repo_url, suffix) if suffix else repo_url
        if kind != "license":
            caveats = ["The repository must be public for the badge to render."]
    params.append("label=%s" % quote(label or default_label))
    if color:
        params.append("color=%s" % color.lstrip("#"))
    url += ("&" if "?" in url else "?") + "&".join(params)
    alt = label or default_label
    return {"kind": kind, "url": url, "link": link, "markdown": "[![%s](%s)](%s)" % (alt, url, link),
            "caveats": caveats}


# --------------------------------------------------------------------------- install-action

WORKFLOW = """name: Showcase counters

on:
  push:
    branches: [{branch}]
  workflow_dispatch:

permissions:
  contents: write

jobs:
  counters:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 1
          filter: blob:none
          sparse-checkout: |
{sparse}
          sparse-checkout-cone-mode: false
      - name: Update counters
        run: python3 .github/scripts/showcase_counts.py sync .
      - name: Commit if changed
        run: |
          if git diff --quiet; then
            echo "Counters are up to date."
            exit 0
          fi
          git config user.name "github-actions[bot]"
          git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
          git commit -am "chore: update counters"
          git push
"""


def sparse_patterns(config):
    """Paths the Action needs on disk (gitignore-style, anchored at the root): the configuration, the vendored
    script, the files whose markers are rewritten and the files read by the lines and json counters. Everything
    else is counted from the git index, so a blobless sparse checkout is enough even on a huge repository."""
    wanted = [CONFIG_NAME, ".github/scripts/"]
    wanted += _as_list(config.get("files")) or DEFAULT_FILES
    for spec in config["counters"].values():
        if isinstance(spec, dict):
            wanted += [str(spec[key]) for key in ("lines", "json") if key in spec]
    patterns = []
    for item in wanted:
        _check_pattern(item)
        if "\n" in item:
            raise CountsError(EXIT_USAGE, "invalid path in %s: %r" % (CONFIG_NAME, item))
        pattern = "/" + (item[2:] if item.startswith("./") else item)
        if pattern not in patterns:
            patterns.append(pattern)
    return patterns


def install_action(root, force=False, branch=None):
    """Copy this script and write the workflow. Nothing is committed: the user reviews and commits."""
    root = Path(root)
    config = load_config(root)
    patterns = sparse_patterns(config)
    script = root / ".github" / "scripts" / "showcase_counts.py"
    workflow = root / ".github" / "workflows" / "showcase-counts.yml"
    existing = [str(p.relative_to(root)) for p in (script, workflow) if p.exists()]
    if existing and not force:
        raise CountsError(EXIT_REFUSED, "already exists: %s (use --force to overwrite)" % ", ".join(existing))
    script.parent.mkdir(parents=True, exist_ok=True)
    workflow.parent.mkdir(parents=True, exist_ok=True)
    source = Path(__file__).resolve()
    if source != script.resolve():
        shutil.copyfile(str(source), str(script))
    script.chmod(0o755)
    sparse = "\n".join("            " + pattern for pattern in patterns)
    workflow.write_text(WORKFLOW.format(branch=json.dumps(branch or _branch(root)), sparse=sparse),
                        encoding="utf-8")
    notes = ["Review the two files, then commit and push them yourself (nothing was committed).",
             "The workflow checks out only %s: if you add markers in other files, list them under \"files\" in %s "
             "and run install-action --force again." % (", ".join(patterns), CONFIG_NAME)]
    return {"written": [str(script.relative_to(root)), str(workflow.relative_to(root))], "overwritten": existing,
            "notes": notes}


# --------------------------------------------------------------------------- command line

def _guard(function):
    def run(args):
        try:
            return function(args)
        except CountsError as error:
            print_json({"ok": False, "error": str(error)})
            return error.code
    return run


def _cmd_sync(args):
    result = sync(_root(args.repo), check=args.check)
    problem = args.check and (result["stale"] or result["unknown"])
    result["ok"] = not problem
    print_json(result)
    return EXIT_FINDINGS if problem else EXIT_OK


def _cmd_lint(args):
    result = lint(_root(args.repo))
    result["ok"] = result["warnings"] == 0
    print_json(result)
    return EXIT_FINDINGS if result["warnings"] else EXIT_OK


def _cmd_badge(args):
    result = make_badge(_root(args.repo), args.kind, args.path, args.label, args.color, args.slug)
    result["ok"] = True
    print_json(result)
    return EXIT_OK


def _cmd_install(args):
    result = install_action(_root(args.repo), args.force, args.branch)
    result["ok"] = True
    print_json(result)
    return EXIT_OK


def _register(subparsers):
    parser = subparsers.add_parser("sync", help="recompute counters and rewrite markers")
    parser.add_argument("repo")
    parser.add_argument("--check", action="store_true", help="write nothing, exit 4 if something is stale")
    parser.set_defaults(func=_guard(_cmd_sync))
    parser = subparsers.add_parser("lint", help="find hard-coded counts in the README and the SVG text")
    parser.add_argument("repo")
    parser.set_defaults(func=_guard(_cmd_lint))
    parser = subparsers.add_parser("badge", help="shields.io badge URL and Markdown")
    parser.add_argument("repo")
    parser.add_argument("--kind", required=True, choices=sorted(BADGE_PATHS) + ["files"])
    parser.add_argument("--path", default="", help="folder for --kind files")
    parser.add_argument("--label")
    parser.add_argument("--color", help="hex color, for example 0a7cff")
    parser.add_argument("--slug", help="OWNER/REPO when there is no GitHub origin")
    parser.set_defaults(func=_guard(_cmd_badge))
    parser = subparsers.add_parser("install-action", help="add the workflow that keeps the counters up to date")
    parser.add_argument("repo")
    parser.add_argument("--force", action="store_true", help="overwrite existing files")
    parser.add_argument("--branch", help="branch that triggers the workflow (default: the default branch)")
    parser.set_defaults(func=_guard(_cmd_install))


def add_commands(subparsers):
    """Register `counts {sync|lint|badge|install-action}` on the dispatcher."""
    parser = subparsers.add_parser("counts", help="live counters instead of hard-coded numbers")
    sub = parser.add_subparsers(dest="counts_command", metavar="<action>")
    sub.required = True
    _register(sub)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="showcase_counts.py", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", metavar="<command>")
    sub.required = True
    _register(sub)
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except Exception as error:  # noqa: BLE001 - reported as JSON, exit 1
        traceback.print_exc(file=sys.stderr)
        print_json({"ok": False, "error": "unexpected: %s: %s" % (type(error).__name__, error)})
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())

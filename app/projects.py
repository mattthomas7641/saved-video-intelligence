"""Your own projects, as context for the research agent.

Scans PROJECTS_DIR (default ~/Projects; mounted read-only at /projects in Docker)
for one folder per project, plus this app itself, and keeps a short "card" per
project: purpose, stack, architecture, what's next. The agent reads the most
relevant cards to judge whether a video belongs in one of your projects, and
can open specific files through `safe_read` to back up its claims.

Hard rule: nothing that could hold a secret is ever read or sent anywhere.
`safe_read` refuses .env files, secrets/ and data/ folders, databases, keys,
VCS and dependency folders, binaries, large files, and any path (including a
symlink) that resolves outside the project folder.
"""
import hashlib
import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlmodel import Session, select

from app import tracing
from app.config import BASE_DIR, PROJECTS_DIR, get_agent_settings, get_gemini_key, get_project_overrides, get_xai_key
from app.models import Project

log = logging.getLogger(__name__)

SELF_NAME = "saved-video-intelligence"   # this app, always included, scanned from its own folder
SKIP_FOLDERS = {"_archive", "mattthomas7641"}  # archive and the GitHub profile-README repo
PLAN_FILE = "PLAN.md"
PLANNED_IDEAS = "planned-ideas"          # pseudo-project fed by Settings + PLAN.md

MAX_FILE_BYTES = 200_000
README_CHARS = 4000
DOC_CHARS = 1500
TREE_LIMIT = 60

_DENY_PARTS = {".git", "node_modules", "venv", ".venv", "env", "secrets", "data", "__pycache__",
               ".pytest_cache", ".ruff_cache", ".mypy_cache", "dist", "build", ".next", "output"}
_DENY_SUFFIXES = {".db", ".sqlite", ".sqlite3", ".pem", ".key", ".p12", ".pfx", ".crt", ".der", ".keystore",
                  ".jks", ".kdbx", ".log", ".coverage"}
_DENY_NAME_RE = re.compile(
    r"secret|credential|auth_state|password|^id_rsa|^tokens?\.(json|txt|ya?ml)$|service[-_]?account", re.I)
_DOC_FILES = ("ARCHITECTURE.md", "DESIGN.md", "PRODUCT.md")
_DEP_FILES = ("requirements.txt", "pyproject.toml", "package.json", "go.mod", "Cargo.toml")


class Denied(ValueError):
    """A path the agent may not read (secret-looking, outside the project, binary, too big)."""


@dataclass
class ProjectCard:
    name: str
    status: str
    card: str


# ---------------- safe file access ----------------

def is_denied(rel: Path) -> bool:
    if any(p in _DENY_PARTS or p.startswith(".") for p in rel.parts) or rel.name in _DENY_PARTS:
        return True  # hidden files cover .env*, .npmrc, .netrc, .git, .coverage
    return rel.suffix.lower() in _DENY_SUFFIXES or bool(_DENY_NAME_RE.search(rel.name))


def safe_read(root: Path, rel_path: str, max_chars: int = 12_000) -> str:
    """Read one text file inside `root`, or raise Denied."""
    root = root.resolve()
    rel = Path(rel_path.strip().lstrip("/"))
    if not rel.parts or ".." in rel.parts:
        raise Denied("Give a path inside the project, like README.md or app/main.py.")
    if is_denied(rel):
        raise Denied(f"{rel_path} may hold secrets or isn't source; it can't be read.")
    target = (root / rel).resolve()
    if not target.is_relative_to(root):
        raise Denied(f"{rel_path} points outside the project.")
    if is_denied(target.relative_to(root)):
        raise Denied(f"{rel_path} may hold secrets or isn't source; it can't be read.")
    if not target.is_file():
        raise Denied(f"{rel_path} doesn't exist. Check the file list in the project card.")
    if target.stat().st_size > MAX_FILE_BYTES:
        raise Denied(f"{rel_path} is too large to read.")
    data = target.read_bytes()
    if b"\0" in data[:4096]:
        raise Denied(f"{rel_path} is a binary file.")
    text = data.decode("utf-8", errors="replace")
    return text if len(text) <= max_chars else text[:max_chars] + "\n…(truncated)"


def file_tree(root: Path, limit: int = TREE_LIMIT) -> list[str]:
    """Readable files, two levels deep, skipping anything safe_read would refuse.
    Walks only those two levels (never descends into .venv, node_modules, data...)."""
    root = root.resolve()
    out: list[str] = []

    def visit(folder: Path, depth: int) -> bool:
        try:
            entries = sorted(folder.iterdir())
        except OSError:
            return True
        for path in entries:
            rel = path.relative_to(root)
            if is_denied(rel) or (path.is_symlink() and not path.resolve().is_relative_to(root)):
                continue
            is_dir = path.is_dir()
            out.append(f"{rel}/" if is_dir else str(rel))
            if len(out) >= limit:
                out.append("…")
                return False
            if is_dir and depth < 2 and not visit(path, depth + 1):
                return False
        return True

    visit(root, 1)
    return out


# ---------------- scanning ----------------

def project_roots(projects_dir: Path = None) -> dict[str, Path]:
    """name -> resolved folder. Top-level entries may be symlinks to where a project
    really lives (~/Projects/stretch-finder -> ~/Sites/body-stretches); each project's
    reads are then confined to that resolved folder by safe_read. This app is always
    included from its own folder, and links that don't resolve (e.g. a target not
    mounted into Docker) are skipped."""
    projects_dir = projects_dir or PROJECTS_DIR
    roots = {SELF_NAME: BASE_DIR.resolve()}
    if projects_dir.is_dir():
        for entry in sorted(projects_dir.iterdir()):
            if entry.name.startswith(".") or entry.name in SKIP_FOLDERS or entry.name == SELF_NAME:
                continue
            target = entry.resolve()
            if not target.is_dir() or target == roots[SELF_NAME]:
                continue
            roots[entry.name] = target
    return roots


def _read_or_empty(root: Path, name: str, chars: int) -> str:
    try:
        return safe_read(root, name, chars)
    except Denied:
        return ""


def scan(root: Path) -> dict:
    """Raw material for one project's card, all through safe_read."""
    readme = _read_or_empty(root, "README.md", README_CHARS)
    docs = {d: _read_or_empty(root, d, DOC_CHARS) for d in _DOC_FILES}
    deps = {d: _read_or_empty(root, d, 2000) for d in _DEP_FILES}
    tree = file_tree(root)
    return {
        "readme": readme,
        "docs": {k: v for k, v in docs.items() if v},
        "deps": {k: v for k, v in deps.items() if v},
        "tree": tree,
        "status": detect_status(tree),
    }


def detect_status(tree: list[str]) -> str:
    """'planned' when the folder is only a README/notes so far (e.g. job-search-agent)."""
    code = [f for f in tree if not f.endswith("/") and not f.lower().endswith((".md", ".txt")) and f != "LICENSE"]
    return "active" if code else "planned"


def raw_text(name: str, scanned: dict) -> str:
    parts = [f"# {name}", scanned["readme"] or "(no README)"]
    for k, v in scanned["docs"].items():
        parts.append(f"## {k}\n{v}")
    for k, v in scanned["deps"].items():
        parts.append(f"## {k}\n{v}")
    parts.append("## Files\n" + "\n".join(scanned["tree"]))
    return "\n\n".join(parts)


def planned_ideas_text(projects_dir: Path = None) -> str:
    projects_dir = projects_dir or PROJECTS_DIR
    plan = _read_or_empty(projects_dir, PLAN_FILE, 6000) if projects_dir.is_dir() else ""
    ideas = get_agent_settings()["planned_ideas"].strip()
    if not plan and not ideas:
        return ""
    return "\n\n".join(x for x in [f"## Ideas not started yet\n{ideas}" if ideas else "",
                                   f"## PLAN.md\n{plan}" if plan else ""] if x)


def _over_cap(session: Session) -> bool:
    return tracing.spend_today(session) >= get_agent_settings()["daily_cap_usd"]


def _card_maker():
    """Whichever cheap model has a key: Gemini first, then Grok; None means README cards."""
    if get_gemini_key():
        from app import gemini_client
        return gemini_client.project_card
    if get_xai_key():
        from app import xai_client
        return xai_client.project_card
    return None


def _fallback_card(name: str, scanned: dict) -> str:
    """Free card when there's no xAI key yet: the README's opening plus the stack."""
    readme = re.sub(r"\s+", " ", scanned["readme"] or "").strip()[:700]
    deps = ", ".join(scanned["deps"]) or "unknown stack"
    return f"{readme or name} (Files: {deps}.)"


def refresh(session: Session, make_card=None, projects_dir: Path = None) -> dict:
    """Rescan every project and rebuild cards whose files changed. `make_card(name, raw)`
    returns (card_text, cost_usd); without one (no xAI key) cards fall back to the README."""
    if make_card is None:
        make_card = _card_maker()
    if make_card is not None and _over_cap(session):
        log.info("Daily spending cap reached: project cards use the free README summary until tomorrow")
        make_card = None
    seen, rebuilt, cost = set(), 0, 0.0
    for name, root in project_roots(projects_dir).items():
        scanned = scan(root)
        changed, card_cost = _upsert(session, name, str(root), scanned["status"], raw_text(name, scanned),
                                     make_card, lambda n=name, s=scanned: _fallback_card(n, s))
        seen.add(name)
        rebuilt, cost = rebuilt + changed, cost + card_cost
    ideas = planned_ideas_text(projects_dir)
    if ideas:
        changed, card_cost = _upsert(session, PLANNED_IDEAS, "", "planned", ideas, make_card, lambda: ideas[:900])
        seen.add(PLANNED_IDEAS)
        rebuilt, cost = rebuilt + changed, cost + card_cost
    for p in session.exec(select(Project)).all():
        if p.name not in seen:
            session.delete(p)
    session.commit()
    return {"projects": len(seen), "rebuilt": rebuilt, "cost_usd": round(cost, 4)}


def _upsert(session: Session, name: str, path: str, status: str, text: str, make_card, fallback) -> tuple[int, float]:
    """Returns (1 if the card was rebuilt else 0, what rebuilding it cost). Commits per
    project, and calls the model with no write pending, so a slow card never holds the
    database lock that the research lane and dashboard need."""
    digest = hashlib.sha256(text.encode()).hexdigest()
    p = session.exec(select(Project).where(Project.name == name)).first()
    needs_card = (p is None or p.content_hash != digest or not p.card
                  or (make_card is not None and p.card_source == "readme"))
    session.rollback()  # end the read before a possibly slow network call
    card, card_cost, source = None, 0.0, "readme"
    if needs_card and make_card is not None:
        try:
            with tracing.run("project_card"):  # counts toward the daily cap like any other spend
                card, card_cost = make_card(name, text)
            source = "model"
        except Exception as e:  # noqa: BLE001 - a card failure must not block the rest
            log.warning("Card for %s failed: %s", name, e)
    p = session.exec(select(Project).where(Project.name == name)).first() or Project(name=name, path=path)
    p.path, p.status, p.scanned_at = path, status, datetime.utcnow()
    if needs_card:
        if card or not p.card or p.content_hash != digest:  # keep a good card if a rebuild failed
            p.card = (card or fallback()).strip()
            p.card_source = source if card else "readme"
            p.cost_usd = card_cost
        p.content_hash = digest
        p.card_updated_at = datetime.utcnow()
    session.add(p)
    session.commit()
    return (1, card_cost) if needs_card else (0, 0.0)


def cards(session: Session) -> list[ProjectCard]:
    """Cards the agent sees, with your Settings overrides applied (ignored ones dropped)."""
    overrides = get_project_overrides()
    out = []
    for p in session.exec(select(Project).order_by(Project.name)).all():
        status = overrides.get(p.name, p.status)
        if status == "ignore" or not p.card:
            continue
        out.append(ProjectCard(name=p.name, status=status, card=p.card))
    return out


def read_project_file(project: str, path: str) -> dict:
    """The agent's tool: read one file from one of your projects, through safe_read."""
    roots = project_roots()
    root = roots.get(project.strip())
    if root is None:
        return {"error": f"No project named {project!r}. Projects: {', '.join(sorted(roots))}."}
    if project.strip() in get_project_overrides() and get_project_overrides()[project.strip()] == "ignore":
        return {"error": f"{project} is excluded in Settings."}
    try:
        content = safe_read(root, path)
    except Denied as e:
        return {"error": str(e)}
    log.info("Agent read %s/%s", project.strip(), path)
    return {"project": project, "path": path, "content": scrub_secrets(content)}


# Credential shapes that could sit in an innocently-named file (config.yaml, settings.py...).
_SECRET_RE = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"
    r"|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_\-]{30,}|sk-(?:ant-|or-|proj-)?[A-Za-z0-9_\-]{20,}|xai-[A-Za-z0-9]{20,}"
    r"|gh[pousr]_[A-Za-z0-9]{30,}|xox[abprs]-[A-Za-z0-9-]{10,}|[0-9]{8,10}:AA[A-Za-z0-9_\-]{30,}"
    r"|(?i:(?:api[_-]?key|secret|token|password)\s*[:=]\s*)[\"']?[A-Za-z0-9_\-/+=.]{16,}")


def scrub_secrets(text: str) -> str:
    """Second line of defence after the deny-list: anything credential-shaped in a file
    the agent reads is replaced before it can reach the model (or a report)."""
    return _SECRET_RE.sub("[REDACTED]", text)


# ---------------- background rescans ----------------

_started = False


def start_rescans(interval_s: int = 24 * 3600) -> None:
    """Rescan at startup and then daily, on a background thread."""
    global _started
    if _started:
        return
    _started = True

    def loop():
        from app.db import get_session
        while True:
            try:
                with get_session() as session:
                    log.info("Project rescan: %s", json.dumps(refresh(session)))
            except Exception:  # noqa: BLE001
                log.exception("Project rescan failed")
            time.sleep(interval_s)

    threading.Thread(target=loop, daemon=True, name="project-rescan").start()

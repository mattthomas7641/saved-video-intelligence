"""app/projects.py: finding your projects, the secret-safe reader, and cards."""
import pytest
from sqlmodel import select

from app import projects
from app.config import save_agent_settings, save_project_override
from app.models import Project


@pytest.fixture
def tree(tmp_path):
    root = tmp_path / "Projects"
    (root / "gateway" / "gateway").mkdir(parents=True)
    (root / "gateway" / "README.md").write_text("# Gateway\nRoutes LLM calls and batches them.")
    (root / "gateway" / "requirements.txt").write_text("fastapi\nhttpx\n")
    (root / "gateway" / "gateway" / "batcher.py").write_text("def batch(): ...\n")
    (root / "gateway" / ".env").write_text("OPENAI_KEY=sk-real")
    (root / "gateway" / "secrets").mkdir()
    (root / "gateway" / "secrets" / "token.json").write_text("{}")
    (root / "gateway" / "data").mkdir()
    (root / "gateway" / "data" / "app.db").write_bytes(b"SQLite\0format")
    (root / "budget-agent").mkdir()
    (root / "budget-agent" / "README.md").write_text("# Budget agent\nPlanned: Plaid + alerts.")
    (root / "_archive" / "old").mkdir(parents=True)
    outside = tmp_path / "elsewhere" / "stretch"
    outside.mkdir(parents=True)
    (outside / "README.md").write_text("# Stretch finder")
    (outside / "generate.py").write_text("print('hi')")
    (root / "stretch-finder").symlink_to(outside)
    (root / "PLAN.md").write_text("Next: build the job search agent.")
    return root


def test_project_roots_follow_links_and_skip_archive(tree):
    roots = projects.project_roots(tree)
    assert {"gateway", "budget-agent", "stretch-finder", projects.SELF_NAME} <= set(roots)
    assert "_archive" not in roots
    assert roots["stretch-finder"] == (tree.parent / "elsewhere" / "stretch").resolve()


def test_status_detection(tree):
    assert projects.scan(tree / "gateway")["status"] == "active"
    assert projects.scan(tree / "budget-agent")["status"] == "planned"


@pytest.mark.parametrize("path", [".env", "secrets/token.json", "data/app.db", "../budget-agent/README.md",
                                  "/etc/passwd", ".git/config", "nothere.py"])
def test_safe_read_refuses(tree, path):
    with pytest.raises(projects.Denied):
        projects.safe_read(tree / "gateway", path)


def test_safe_read_refuses_symlink_escape(tree, tmp_path):
    (tmp_path / "secret.txt").write_text("nope")
    (tree / "gateway" / "notes.txt").symlink_to(tmp_path / "secret.txt")
    with pytest.raises(projects.Denied):
        projects.safe_read(tree / "gateway", "notes.txt")


def test_safe_read_reads_source(tree):
    assert "def batch" in projects.safe_read(tree / "gateway", "gateway/batcher.py")


def test_scan_never_includes_secrets(tree):
    text = projects.raw_text("gateway", projects.scan(tree / "gateway"))
    assert "sk-real" not in text and ".env" not in text and "token.json" not in text and "app.db" not in text
    assert "batcher.py" in text and "fastapi" in text


def test_refresh_builds_cards_once_per_change(tree, session):
    calls = []
    def make_card(name, raw):
        calls.append(name)
        return f"card for {name}", 0.002
    first = projects.refresh(session, make_card=make_card, projects_dir=tree)
    assert "gateway" in calls and "planned-ideas" in calls  # PLAN.md feeds the planned-ideas card
    assert first["rebuilt"] == len(calls)
    calls.clear()
    assert projects.refresh(session, make_card=make_card, projects_dir=tree)["rebuilt"] == 0 and calls == []
    (tree / "gateway" / "README.md").write_text("# Gateway v2")
    projects.refresh(session, make_card=make_card, projects_dir=tree)
    assert calls == ["gateway"]


def test_refresh_without_xai_key_uses_readme_cards(tree, session):
    projects.refresh(session, projects_dir=tree)
    p = session.exec(select(Project).where(Project.name == "gateway")).one()
    assert p.card_source == "readme" and "Routes LLM calls" in p.card


def test_planned_ideas_from_settings(tree, session):
    save_agent_settings(planned_ideas="A Plaid budget agent")
    projects.refresh(session, make_card=lambda n, raw: (raw, 0.0), projects_dir=tree)
    card = session.exec(select(Project).where(Project.name == "planned-ideas")).one().card
    assert "Plaid budget agent" in card and "job search agent" in card


def test_cards_apply_overrides(tree, session):
    projects.refresh(session, make_card=lambda n, raw: (f"card {n}", 0.0), projects_dir=tree)
    save_project_override("gateway", "ignore")
    save_project_override("budget-agent", "active")
    by_name = {c.name: c for c in projects.cards(session)}
    assert "gateway" not in by_name and by_name["budget-agent"].status == "active"


def test_read_project_file_tool(monkeypatch, tree):
    monkeypatch.setattr(projects, "PROJECTS_DIR", tree)
    assert "def batch" in projects.read_project_file("gateway", "gateway/batcher.py")["content"]
    assert "error" in projects.read_project_file("gateway", ".env")
    assert "No project named" in projects.read_project_file("nope", "README.md")["error"]


def test_card_generation_never_holds_the_database_lock(tree, session):
    """Regression: refresh() used to keep a write pending while waiting on the model,
    so the research lane hit 'database is locked' during a rescan."""
    import sqlite3

    from app.config import DB_PATH

    def make_card(name, raw):
        other = sqlite3.connect(DB_PATH, timeout=0.5)  # fails fast if a write lock is held
        other.execute("UPDATE project SET status = status")
        other.commit()
        other.close()
        return f"card {name}", 0.001

    projects.refresh(session, make_card=make_card, projects_dir=tree)  # first pass: inserts
    (tree / "gateway" / "README.md").write_text("# Gateway v3")
    projects.refresh(session, make_card=make_card, projects_dir=tree)  # second pass: updates

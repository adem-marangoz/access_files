from __future__ import annotations

from pathlib import Path

from conftest import git, requires_git

from tpms_agent.git_monitor import GitRepo, LineStats, parse_name_status, parse_numstat, parse_status

pytestmark = requires_git


def test_parse_status_handles_renames_and_spaces():
    output = b" M fw/sp ace.c\0RM fw/src/b.c\0fw/src/a.c\0?? fw/new.c\0"
    entries = parse_status(output)
    assert [(e.code, e.path, e.orig_path) for e in entries] == [
        (" M", "fw/sp ace.c", None),
        ("RM", "fw/src/b.c", "fw/src/a.c"),
        ("??", "fw/new.c", None),
    ]
    assert [e.letter for e in entries] == ["M", "R", "A"]


def test_parse_numstat_and_name_status():
    assert parse_numstat(b"3\t1\ta.c\0-\t-\tlogo.png\0") == {
        "a.c": LineStats(3, 1),
        "logo.png": LineStats(None, None),
    }
    assert parse_name_status(b"M\0a.c\0D\0old dir/x.c\0") == {"a.c": "M", "old dir/x.c": "D"}


def test_not_a_repository(tmp_path: Path):
    assert GitRepo.discover(tmp_path) is None


def test_branch_status_stats_and_diff(make_repo):
    repo_path = make_repo(files={"src/a.c": "int a;\n", "src/b.c": "one\ntwo\n"})
    (repo_path / "src" / "a.c").write_text("int a;\nint b;\nint c;\n")
    (repo_path / "src" / "b.c").unlink()
    (repo_path / "src" / "new file.c").write_text("x\ny\nz")  # no trailing newline

    repo = GitRepo.discover(repo_path)
    assert repo is not None and repo.prefix == ""
    assert repo.branch() == "main"
    assert len(repo.head()) >= 40

    status = {e.path: e.kind for e in repo.status()}
    assert status == {"src/a.c": "modified", "src/b.c": "deleted", "src/new file.c": "untracked"}

    assert repo.name_status() == {"src/a.c": "M", "src/b.c": "D"}
    assert repo.numstat() == {"src/a.c": LineStats(2, 0), "src/b.c": LineStats(0, 2)}
    assert repo.numstat(["src/a.c"]) == {"src/a.c": LineStats(2, 0)}
    assert repo.untracked_stats("src/new file.c") == LineStats(3, 0)

    patch = repo.diff(["src/a.c"])
    assert "+int b;" in patch and "src/b.c" not in patch

    untracked = repo.untracked_patch("src/new file.c")
    assert untracked.startswith("diff --git a/src/new file.c b/src/new file.c\nnew file mode")
    assert "@@ -0,0 +1,3 @@\n+x\n+y\n+z\n\\ No newline at end of file\n" in untracked


def test_untracked_patch_applies_with_git(make_repo, tmp_path: Path):
    repo_path = make_repo(files={"README": "hi\n"})
    (repo_path / "added.txt").write_text("line 1\nline 2\n")
    patch = GitRepo.discover(repo_path).untracked_patch("added.txt")
    (repo_path / "added.txt").unlink()
    patch_file = tmp_path / "x.patch"
    patch_file.write_text(patch)
    git(repo_path, "apply", "--check", str(patch_file))


def test_project_in_subdirectory_is_scoped(make_repo):
    repo_path = make_repo(files={"fw/main.c": "a\n", "docs/readme.md": "b\n"})
    (repo_path / "fw" / "main.c").write_text("a\nchanged\n")
    (repo_path / "docs" / "readme.md").write_text("changed\n")
    repo = GitRepo.discover(repo_path / "fw")
    assert repo.prefix == "fw"
    assert repo.to_repo_path("main.c") == "fw/main.c"
    assert repo.to_project_path("fw/main.c") == "main.c"
    assert [e.path for e in repo.status()] == ["fw/main.c"]
    assert list(repo.numstat()) == ["fw/main.c"]


def test_repository_without_commits(make_repo):
    repo_path = make_repo(files={"a.c": "x\n"}, commit=False)
    git(repo_path, "add", "a.c")
    repo = GitRepo.discover(repo_path)
    assert repo.head() is None
    assert repo.branch() == "main"
    assert repo.name_status() == {"a.c": "A"}
    assert repo.numstat() == {"a.c": LineStats(1, 0)}


def test_diff_against_older_base_includes_committed_work(make_repo):
    repo_path = make_repo(files={"a.c": "1\n"})
    repo = GitRepo.discover(repo_path)
    start = repo.head()
    (repo_path / "a.c").write_text("1\n2\n")
    git(repo_path, "commit", "-qam", "second")
    (repo_path / "a.c").write_text("1\n2\n3\n")
    assert repo.numstat() == {"a.c": LineStats(1, 0)}
    assert repo.numstat(base=start) == {"a.c": LineStats(2, 0)}
    assert repo.commits_between(start, repo.head()) == [f"{repo.head()[:7]} second"]


def test_many_paths_are_chunked(make_repo):
    files = {f"f{i:03}.txt": "x\n" for i in range(250)}
    repo_path = make_repo(files=files)
    for name in files:
        (repo_path / name).write_text("y\n")
    repo = GitRepo.discover(repo_path)
    assert len(repo.numstat(sorted(files))) == 250


def test_paths_with_glob_characters_are_literal(make_repo):
    repo_path = make_repo(files={"a[1].c": "x\n", "a1.c": "x\n"})
    (repo_path / "a[1].c").write_text("y\n")
    (repo_path / "a1.c").write_text("y\n")
    repo = GitRepo.discover(repo_path)
    assert list(repo.numstat(["a[1].c"])) == ["a[1].c"]

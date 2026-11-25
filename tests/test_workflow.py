import os
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


@unittest.skipUnless(shutil.which("git") and shutil.which("bash"), "git and bash are required")
class PublishingTests(unittest.TestCase):
    def git(self, directory, *args):
        return subprocess.run(
            ["git", *args], cwd=directory, check=True, capture_output=True, text=True,
        ).stdout.strip()

    def test_publish_handles_concurrent_changes_without_overwriting_them(self):
        workflow = Path(__file__).resolve().parents[1] / ".github/workflows/main.yml"
        script = textwrap.dedent(workflow.read_text(encoding="utf-8").split(
            "    - name: Commit and push if changed\n", 1,
        )[1].split("      run: |\n", 1)[1])
        for conflict in (False, True):
            with self.subTest(conflict=conflict), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                remote, seed, runner, writer = (root / name for name in ("remote", "seed", "runner", "writer"))
                self.git(root, "init", "--bare", str(remote))
                self.git(root, "init", "-b", "main", str(seed))
                self.git(seed, "config", "user.name", "Test")
                self.git(seed, "config", "user.email", "test@example.com")
                for country in ("in", "us", "gb", "ca", "au", "data"):
                    (seed / country).mkdir()
                    (seed / country / "feed.json").write_text("[]\n", encoding="utf-8")
                (seed / "README.md").write_text("original\n", encoding="utf-8")
                self.git(seed, "add", ".")
                self.git(seed, "commit", "-m", "initial")
                self.git(seed, "remote", "add", "origin", str(remote))
                self.git(seed, "push", "-u", "origin", "main")
                for checkout in (runner, writer):
                    self.git(root, "clone", "--branch", "main", str(remote), str(checkout))
                self.git(writer, "config", "user.name", "Test")
                self.git(writer, "config", "user.email", "test@example.com")
                (runner / "data/feed.json").write_text('["scraped"]\n', encoding="utf-8")
                (runner / "local-notes.txt").write_text("do not publish", encoding="utf-8")
                changed = "data/feed.json" if conflict else "README.md"
                (writer / changed).write_text('["concurrent"]\n', encoding="utf-8")
                self.git(writer, "add", changed)
                self.git(writer, "commit", "-m", "concurrent update")
                self.git(writer, "push")
                environment = {**os.environ, "GITHUB_REF_NAME": "main"}
                result = subprocess.run(
                    [shutil.which("bash"), "-e", "-o", "pipefail", "-c", script],
                    cwd=runner, env=environment, capture_output=True, text=True,
                )
                self.assertEqual(result.returncode == 0, not conflict, result.stdout + result.stderr)
                self.assertEqual(self.git(remote, "show", "main:" + changed), '["concurrent"]')
                self.assertNotIn("local-notes.txt", self.git(remote, "ls-tree", "--name-only", "main"))
                if not conflict:
                    self.assertEqual(self.git(remote, "show", "main:data/feed.json"), '["scraped"]')
                    head = self.git(remote, "rev-parse", "main")
                    subprocess.run(
                        [shutil.which("bash"), "-e", "-o", "pipefail", "-c", script],
                        cwd=runner, env=environment, check=True, capture_output=True,
                    )
                    self.assertEqual(self.git(remote, "rev-parse", "main"), head)


if __name__ == "__main__":
    unittest.main()

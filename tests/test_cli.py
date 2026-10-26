import contextlib
import gzip
import io
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_loader = SourceFileLoader("ghcd", os.path.join(ROOT, "gh-cache-download"))
ghcd = module_from_spec(spec_from_loader("ghcd", _loader))
_loader.exec_module(ghcd)


def entry(key="k", ref="refs/heads/main", version="v1" * 32, id=1, size=10):
    return {
        "id": id,
        "key": key,
        "ref": ref,
        "version": version,
        "size_in_bytes": size,
        "created_at": "2026-10-26T00:00:00Z",
    }


class SelectCache(unittest.TestCase):
    def test_exact_key_only(self):
        caches = [entry(key="npm-abc"), entry(key="npm-abcdef", id=2)]
        self.assertEqual(ghcd.select_cache(caches, "npm-abc")["id"], 1)

    def test_missing_key_lists_prefix_matches(self):
        with self.assertRaisesRegex(ghcd.Error, "npm-abcdef"):
            ghcd.select_cache([entry(key="npm-abcdef")], "npm-abc")

    def test_ambiguous_needs_ref(self):
        caches = [entry(), entry(ref="refs/heads/dev", id=2)]
        with self.assertRaisesRegex(ghcd.Error, "2 entries"):
            ghcd.select_cache(caches, "k")
        self.assertEqual(ghcd.select_cache(caches, "k", ref="dev")["id"], 2)
        self.assertEqual(ghcd.select_cache(caches, "k", ref="refs/heads/main")["id"], 1)

    def test_version_prefix(self):
        caches = [entry(version="aaaa1111"), entry(version="bbbb2222", id=2)]
        self.assertEqual(ghcd.select_cache(caches, "k", version="bbbb")["id"], 2)


class DispatchRef(unittest.TestCase):
    def test_branch_and_tag(self):
        self.assertEqual(ghcd.dispatch_ref("refs/heads/feature/x"), "feature/x")
        self.assertEqual(ghcd.dispatch_ref("refs/tags/v1"), "v1")

    def test_pull_request_ref_is_refused(self):
        with self.assertRaisesRegex(ghcd.Error, "pull request"):
            ghcd.dispatch_ref("refs/pull/5/merge")


class MemberNames(unittest.TestCase):
    def test_mapping(self):
        f = ghcd.safe_member_name
        self.assertEqual(f("node_modules/a.js"), "node_modules/a.js")
        self.assertEqual(f("../../../.npm/_cacache/x"), "__parent__/__parent__/__parent__/.npm/_cacache/x")
        self.assertEqual(f("/abs/path"), "abs/path")
        self.assertEqual(f("./a/./b"), "a/b")
        self.assertEqual(f("C:\\Users\\x"), "C:/Users/x")
        self.assertEqual(f("."), "")


def build_tar(entries):
    """entries: list of (name, kind, payload) with kind in file/dir/symlink/hardlink."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tf:
        for name, kind, payload in entries:
            info = tarfile.TarInfo(name)
            if kind == "file":
                data = payload.encode()
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))
                continue
            if kind == "dir":
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
            elif kind == "symlink":
                info.type = tarfile.SYMTYPE
                info.linkname = payload
            elif kind == "hardlink":
                info.type = tarfile.LNKTYPE
                info.linkname = payload
            tf.addfile(info)
    return buf.getvalue()


SAMPLE = [
    ("node_modules", "dir", None),
    ("node_modules/a.js", "file", "a"),
    ("node_modules/.bin/a", "symlink", "../a.js"),
    ("node_modules/b.js", "hardlink", "node_modules/a.js"),
    ("../../../.npm/_cacache/index", "file", "npm"),
    ("evil", "symlink", "/etc/passwd"),
]


class Extract(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)

    def check(self, archive):
        dest = os.path.join(self.tmp, "out")
        with contextlib.redirect_stdout(io.StringIO()):
            count, skipped = ghcd.extract(archive, dest, log=lambda m: None)
        self.assertEqual((count, skipped), (5, 1))
        with open(os.path.join(dest, "node_modules/a.js")) as f:
            self.assertEqual(f.read(), "a")
        with open(os.path.join(dest, "node_modules/.bin/a")) as f:
            self.assertEqual(f.read(), "a")
        with open(os.path.join(dest, "node_modules/b.js")) as f:
            self.assertEqual(f.read(), "a")
        with open(os.path.join(dest, "__parent__/__parent__/__parent__/.npm/_cacache/index")) as f:
            self.assertEqual(f.read(), "npm")
        self.assertFalse(os.path.lexists(os.path.join(dest, "evil")))

    def write(self, name, data):
        path = os.path.join(self.tmp, name)
        with open(path, "wb") as f:
            f.write(data)
        return path

    def test_plain_tar(self):
        self.check(self.write("cache.tar", build_tar(SAMPLE)))

    def test_gzip(self):
        self.check(self.write("cache.tgz", gzip.compress(build_tar(SAMPLE))))

    @unittest.skipUnless(shutil.which("zstd"), "zstd not installed")
    def test_zstd(self):
        raw = build_tar(SAMPLE)
        data = subprocess.run(["zstd", "-q", "-c", "--long=30"], input=raw, capture_output=True, check=True).stdout
        self.assertEqual(data[:4], ghcd.ZSTD_MAGIC)
        self.check(self.write("cache.tzst", data))


class FakeGh:
    """Stands in for the gh() helper and records the calls."""

    def __init__(self, caches, artifact_dir_files, run_fails=False, dispatch_fails=False):
        self.caches = caches
        self.files = artifact_dir_files
        self.run_fails = run_fails
        self.dispatch_fails = dispatch_fails
        self.calls = []
        self.request = None

    def __call__(self, *args, check=True, timeout=120):
        self.calls.append(args)
        rc, out, err = 0, "", ""
        if args[0] == "api":
            out = "\n".join(json.dumps(c) for c in self.caches)
        elif args[:2] == ("workflow", "run"):
            if self.dispatch_fails:
                rc, err = 1, "could not create workflow dispatch event: HTTP 404"
            self.request = next(a for a in args if a.startswith("request=")).split("=", 1)[1]
        elif args[:2] == ("run", "list"):
            out = json.dumps([
                {"databaseId": 7, "displayTitle": "cache-download other"},
                {"databaseId": 42, "displayTitle": f"cache-download {self.request}"},
            ])
        elif args[:2] == ("run", "watch"):
            rc = 1 if self.run_fails else 0
        elif args[:2] == ("run", "download"):
            dest = args[args.index("-D") + 1]
            for name, data in self.files.items():
                with open(os.path.join(dest, name), "wb") as f:
                    f.write(data)
        if check and rc != 0:
            raise ghcd.Error(err)
        return subprocess.CompletedProcess(args, rc, out, err)


class GetCommand(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.orig = ghcd.gh
        self.addCleanup(setattr, ghcd, "gh", self.orig)

    def run_main(self, fake, *argv):
        ghcd.gh = fake
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(err):
            rc = ghcd.main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def test_downloads_and_extracts(self):
        cache = entry(key="pip-1", ref="refs/heads/dev", id=99)
        fake = FakeGh([cache, entry(key="pip-12")], {"cache.tgz": gzip.compress(build_tar(SAMPLE[:2]))})
        out_dir = os.path.join(self.tmp, "dl")
        rc, out, err = self.run_main(fake, "get", "pip-1", "-R", "o/r", "-D", out_dir, "-x")
        self.assertEqual(rc, 0, err)
        dispatch = next(c for c in fake.calls if c[:2] == ("workflow", "run"))
        self.assertIn("dev", dispatch)
        self.assertIn(f"version={cache['version']}", dispatch)
        download = next(c for c in fake.calls if c[:2] == ("run", "download"))
        self.assertEqual(download[2], "42")
        self.assertIn(f"cache-{fake.request}", download)
        with open(os.path.join(out_dir, "files/node_modules/a.js")) as f:
            self.assertEqual(f.read(), "a")
        self.assertIn("Archive:", out)

    def test_key_is_url_encoded(self):
        fake = FakeGh([], {})
        self.run_main(fake, "get", "a b&c", "-R", "o/r")
        self.assertIn("key=a%20b%26c", fake.calls[0][2])

    def test_dispatch_failure_points_to_init(self):
        fake = FakeGh([entry()], {}, dispatch_fails=True)
        rc, _, err = self.run_main(fake, "get", "k", "-R", "o/r", "-D", self.tmp)
        self.assertEqual(rc, 1)
        self.assertIn("gh cache-download init", err)

    def test_failed_run(self):
        fake = FakeGh([entry()], {}, run_fails=True)
        rc, _, err = self.run_main(fake, "get", "k", "-R", "o/r", "-D", self.tmp)
        self.assertEqual(rc, 1)
        self.assertIn("--log-failed", err)

    def test_find_run_gives_up(self):
        ghcd.gh = lambda *a, **k: subprocess.CompletedProcess(a, 0, "[]", "")
        with self.assertRaisesRegex(ghcd.Error, "did not appear"):
            ghcd.find_run("o/r", "abc", attempts=3, sleep=lambda s: None)


class Init(unittest.TestCase):
    def test_writes_template_once(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ghcd.main(["init", "--dir", tmp]), 0)
        with open(os.path.join(tmp, ghcd.WORKFLOW_PATH)) as f:
            self.assertEqual(f.read(), ghcd.workflow_text())
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(ghcd.main(["init", "--dir", tmp]), 1)

    def test_repo_workflow_matches_template(self):
        with open(os.path.join(ROOT, ghcd.WORKFLOW_PATH)) as f:
            self.assertEqual(f.read(), ghcd.workflow_text())


if __name__ == "__main__":
    unittest.main()

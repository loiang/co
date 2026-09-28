import hashlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from codex_package import v8
from codex_package.targets import TARGET_SPECS, TargetSpec


class _Response:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def __enter__(self) -> io.BytesIO:
        return io.BytesIO(self.payload)

    def __exit__(self, *_args: object) -> None:
        return None


class FetchCodexV8ArtifactsTest(unittest.TestCase):
    version = "150.4.0"

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def release_fixture(
        self,
        target: str,
        *,
        line_ending: bytes = b"\n",
        trusted_digest: str | None = None,
        trusted_name: str | None = None,
        create_pins: bool = True,
    ) -> tuple[TargetSpec, dict[str, bytes], Path, MagicMock]:
        spec = TARGET_SPECS[target]
        profile = v8.V8_ARTIFACT_PROFILE
        archive_name = (
            f"rusty_v8_{profile}_{target}.lib.gz"
            if spec.is_windows
            else f"librusty_v8_{profile}_{target}.a.gz"
        )
        binding_name = f"src_binding_{profile}_{target}.rs"
        manifest_name = f"rusty_v8_{profile}_{target}.sha256"
        archive = b"trusted V8 archive"
        binding = b"trusted V8 binding"
        manifest = (
            line_ending.join(
                (
                    f"{hashlib.sha256(archive).hexdigest()}  {archive_name}".encode(),
                    f"{hashlib.sha256(binding).hexdigest()}  {binding_name}".encode(),
                )
            )
            + line_ending
        )
        payloads = {
            manifest_name: manifest,
            archive_name: archive,
            binding_name: binding,
        }
        if create_pins:
            pins = (
                self.root / "third_party/v8/rusty_v8_150_4_0_release_manifests.sha256"
            )
            pins.parent.mkdir(parents=True)
            digest = trusted_digest or hashlib.sha256(manifest).hexdigest()
            name = trusted_name or manifest_name
            pins.write_bytes(f"{digest}  {name}".encode() + line_ending)

        def open_url(url: str, **_kwargs: object) -> _Response:
            return _Response(payloads[url.rsplit("/", maxsplit=1)[-1]])

        cache_dir = self.root / "cache" / f"rusty-v8-{self.version}-{target}"
        root_patcher = patch.object(v8, "REPO_ROOT", self.root)
        root_patcher.start()
        self.addCleanup(root_patcher.stop)
        patcher = patch.object(v8, "urlopen", side_effect=open_url)
        urlopen = patcher.start()
        self.addCleanup(patcher.stop)
        return spec, payloads, cache_dir, urlopen

    def _run(
        self,
        spec: TargetSpec,
        *,
        cache_dir: Path,
        sleep: MagicMock | None = None,
        max_attempts: int = 3,
    ) -> v8.RustyV8ArtifactPair:
        return v8.fetch_codex_v8_artifacts(
            spec,
            version=self.version,
            cache_root=cache_dir.parent,
            sleep=sleep,
            max_attempts=max_attempts,
        )

    def _populate_cache(self, cache_dir: Path, payloads: dict[str, bytes]) -> None:
        cache_dir.mkdir(parents=True)
        for name, payload in payloads.items():
            (cache_dir / name).write_bytes(payload)

    def test_fetches_artifacts_after_authenticating_manifest(self) -> None:
        spec, payloads, cache_dir, urlopen = self.release_fixture(
            "x86_64-unknown-linux-gnu"
        )
        artifacts = self._run(spec, cache_dir=cache_dir)
        self.assertEqual(
            artifacts.archive.read_bytes(), payloads[artifacts.archive.name]
        )
        self.assertEqual(
            artifacts.binding.read_bytes(), payloads[artifacts.binding.name]
        )
        self.assertEqual(urlopen.call_count, 3)

    def test_complete_cache_hit_is_offline(self) -> None:
        spec, payloads, cache_dir, _urlopen = self.release_fixture(
            "x86_64-unknown-linux-gnu"
        )
        self._populate_cache(cache_dir, payloads)
        with patch.object(v8, "urlopen", side_effect=AssertionError("network")):
            artifacts = self._run(spec, cache_dir=cache_dir)
        self.assertEqual(
            artifacts.archive.read_bytes(), payloads[artifacts.archive.name]
        )

    def test_retries_connection_reset_then_succeeds(self) -> None:
        spec, payloads, cache_dir, _urlopen = self.release_fixture(
            "x86_64-unknown-linux-gnu"
        )
        attempts = 0

        def open_url(url: str, **_kwargs: object) -> _Response:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise ConnectionResetError("reset")
            return _Response(payloads[url.rsplit("/", maxsplit=1)[-1]])

        sleeps = MagicMock()
        with patch.object(v8, "urlopen", side_effect=open_url) as urlopen:
            self._run(spec, cache_dir=cache_dir, sleep=sleeps)
        self.assertEqual(urlopen.call_count, 4)
        self.assertEqual(sleeps.call_count, 1)

    def test_retries_checksum_failure_then_succeeds(self) -> None:
        spec, payloads, cache_dir, _urlopen = self.release_fixture(
            "x86_64-unknown-linux-gnu"
        )
        archive_name = next(
            name for name in payloads if name.startswith("librusty_v8_")
        )
        calls: dict[str, int] = {}

        def open_url(url: str, **_kwargs: object) -> _Response:
            name = url.rsplit("/", maxsplit=1)[-1]
            calls[name] = calls.get(name, 0) + 1
            if name == archive_name and calls[name] == 1:
                return _Response(b"bad archive")
            return _Response(payloads[name])

        sleeps = MagicMock()
        with patch.object(v8, "urlopen", side_effect=open_url):
            artifacts = self._run(spec, cache_dir=cache_dir, sleep=sleeps)
        self.assertEqual(artifacts.archive.read_bytes(), payloads[archive_name])
        self.assertEqual(calls[archive_name], 2)
        self.assertEqual(sleeps.call_count, 1)

    def test_failed_download_preserves_formal_file_and_cleans_temporary(self) -> None:
        spec, payloads, cache_dir, _urlopen = self.release_fixture(
            "x86_64-unknown-linux-gnu"
        )
        self._populate_cache(cache_dir, payloads)
        archive = next(cache_dir.glob("librusty_v8_*.a.gz"))
        old_archive = b"old corrupt formal file"
        archive.write_bytes(old_archive)
        sleeps = MagicMock()
        with patch.object(v8, "urlopen", return_value=_Response(b"bad archive")):
            with self.assertRaisesRegex(RuntimeError, "checksum validation"):
                self._run(spec, cache_dir=cache_dir, sleep=sleeps)
        self.assertEqual(archive.read_bytes(), old_archive)
        self.assertEqual(list(cache_dir.glob(".*.tmp")), [])
        self.assertEqual(sleeps.call_count, 2)

    def test_retry_limit_is_three_attempts(self) -> None:
        spec, _payloads, cache_dir, _urlopen = self.release_fixture(
            "x86_64-unknown-linux-gnu"
        )
        sleeps = MagicMock()
        with patch.object(
            v8, "urlopen", side_effect=ConnectionResetError("reset")
        ) as urlopen:
            with self.assertRaises(ConnectionResetError):
                self._run(spec, cache_dir=cache_dir, sleep=sleeps)
        self.assertEqual(urlopen.call_count, 3)
        self.assertEqual(sleeps.call_count, 2)
        self.assertEqual(list(cache_dir.glob(".*.tmp")), [])

    def test_non_retryable_http_error_is_immediate(self) -> None:
        spec, _payloads, cache_dir, _urlopen = self.release_fixture(
            "x86_64-unknown-linux-gnu"
        )
        error = HTTPError(
            "https://example.invalid/manifest", 404, "missing", {}, io.BytesIO()
        )
        sleeps = MagicMock()
        with patch.object(v8, "urlopen", side_effect=error) as urlopen:
            with self.assertRaises(HTTPError):
                self._run(spec, cache_dir=cache_dir, sleep=sleeps)
        urlopen.assert_called_once()
        sleeps.assert_not_called()

    def test_authenticates_windows_manifest_with_crlf(self) -> None:
        spec, payloads, cache_dir, urlopen = self.release_fixture(
            "x86_64-pc-windows-msvc", line_ending=b"\r\n"
        )
        artifacts = self._run(spec, cache_dir=cache_dir)
        self.assertEqual(
            artifacts.archive.read_bytes(), payloads[artifacts.archive.name]
        )
        self.assertEqual(
            artifacts.binding.read_bytes(), payloads[artifacts.binding.name]
        )
        self.assertEqual(urlopen.call_count, 3)

    def test_rejects_tampered_manifest_before_downloading_artifacts(self) -> None:
        spec, _payloads, cache_dir, urlopen = self.release_fixture(
            "x86_64-unknown-linux-gnu", trusted_digest="0" * 64
        )
        with self.assertRaisesRegex(RuntimeError, "does not match its trusted SHA-256"):
            self._run(spec, cache_dir=cache_dir)
        self.assertEqual(urlopen.call_count, v8.MAX_DOWNLOAD_ATTEMPTS)

    def test_rejects_missing_manifest_pin_before_downloading(self) -> None:
        spec, _payloads, cache_dir, urlopen = self.release_fixture(
            "x86_64-unknown-linux-gnu", create_pins=False
        )
        with self.assertRaises(FileNotFoundError):
            self._run(spec, cache_dir=cache_dir)
        urlopen.assert_not_called()

    def test_verified_cache_needs_no_network(self) -> None:
        spec, payloads, cache_dir, _urlopen = self.release_fixture(
            "x86_64-unknown-linux-gnu"
        )
        self._populate_cache(cache_dir, payloads)
        with patch.object(v8, "urlopen", side_effect=AssertionError("network")):
            artifacts = self._run(spec, cache_dir=cache_dir)
        self.assertEqual(
            (artifacts.archive.read_bytes(), artifacts.binding.read_bytes()),
            (payloads[artifacts.archive.name], payloads[artifacts.binding.name]),
        )

    def test_missing_or_corrupt_manifest_refresh_retains_valid_artifacts(self) -> None:
        spec, payloads, cache_dir, urlopen = self.release_fixture(
            "x86_64-unknown-linux-gnu"
        )
        artifacts = self._run(spec, cache_dir=cache_dir)
        manifest = cache_dir / next(name for name in payloads if name.endswith(".sha256"))
        expected = manifest.read_bytes()
        for contents in (None, b"corrupt"):
            with self.subTest(contents=contents):
                if contents is None:
                    manifest.unlink()
                else:
                    manifest.write_bytes(contents)
                urlopen.reset_mock()
                refreshed = self._run(spec, cache_dir=cache_dir)
                self.assertEqual(refreshed, artifacts)
                self.assertEqual(manifest.read_bytes(), expected)
                self.assertEqual(urlopen.call_count, 1)

    def test_cached_manifest_still_authenticates_both_artifacts(self) -> None:
        spec, payloads, cache_dir, urlopen = self.release_fixture(
            "x86_64-unknown-linux-gnu"
        )
        artifacts = self._run(spec, cache_dir=cache_dir)
        artifacts.archive.write_bytes(b"corrupt archive")
        artifacts.binding.write_bytes(b"corrupt binding")
        urlopen.reset_mock()
        repaired = self._run(spec, cache_dir=cache_dir)
        self.assertEqual(repaired, artifacts)
        self.assertEqual(
            (repaired.archive.read_bytes(), repaired.binding.read_bytes()),
            (payloads[repaired.archive.name], payloads[repaired.binding.name]),
        )
        self.assertEqual(urlopen.call_count, 2)

    def test_changed_repository_pin_rejects_old_cache_and_bad_refresh(self) -> None:
        spec, payloads, cache_dir, urlopen = self.release_fixture(
            "x86_64-unknown-linux-gnu"
        )
        artifacts = self._run(spec, cache_dir=cache_dir)
        manifest = cache_dir / next(name for name in payloads if name.endswith(".sha256"))
        old_manifest = manifest.read_bytes()
        pins = self.root / "third_party/v8/rusty_v8_150_4_0_release_manifests.sha256"
        pins.write_text(f"{'0' * 64}  {manifest.name}\n")
        urlopen.reset_mock()
        with self.assertRaisesRegex(RuntimeError, "does not match its trusted SHA-256"):
            self._run(spec, cache_dir=cache_dir)
        self.assertEqual(urlopen.call_count, v8.MAX_DOWNLOAD_ATTEMPTS)
        self.assertEqual(manifest.read_bytes(), old_manifest)
        self.assertEqual(
            (artifacts.archive.read_bytes(), artifacts.binding.read_bytes()),
            (payloads[artifacts.archive.name], payloads[artifacts.binding.name]),
        )

    @unittest.skipIf(sys.platform == "win32", "requires unprivileged symlinks")
    def test_symlink_manifest_is_replaced_without_writing_through_link(self) -> None:
        spec, payloads, cache_dir, urlopen = self.release_fixture(
            "x86_64-unknown-linux-gnu"
        )
        artifacts = self._run(spec, cache_dir=cache_dir)
        manifest = cache_dir / next(name for name in payloads if name.endswith(".sha256"))
        expected = manifest.read_bytes()
        backing = manifest.with_suffix(".original")
        manifest.rename(backing)
        manifest.symlink_to(backing)
        urlopen.reset_mock()
        refreshed = self._run(spec, cache_dir=cache_dir)
        self.assertEqual(refreshed, artifacts)
        self.assertFalse(manifest.is_symlink())
        self.assertEqual((manifest.read_bytes(), backing.read_bytes()), (expected, expected))
        self.assertEqual(urlopen.call_count, 1)

    def test_interrupted_refresh_preserves_cache(self) -> None:
        class InterruptedResponse(io.BytesIO):
            def read(self, size=-1):
                if self.tell():
                    raise OSError("interrupted download")
                return super().read(size)

        spec, payloads, cache_dir, _urlopen = self.release_fixture(
            "x86_64-unknown-linux-gnu"
        )
        artifacts = self._run(spec, cache_dir=cache_dir)
        manifest = cache_dir / next(name for name in payloads if name.endswith(".sha256"))
        manifest.write_bytes(b"stale manifest")
        with (
            patch.object(v8, "urlopen", return_value=InterruptedResponse(b"partial")),
            self.assertRaisesRegex(OSError, "interrupted download"),
        ):
            self._run(spec, cache_dir=cache_dir)
        self.assertEqual(manifest.read_bytes(), b"stale manifest")
        self.assertEqual(
            (artifacts.archive.read_bytes(), artifacts.binding.read_bytes()),
            (payloads[artifacts.archive.name], payloads[artifacts.binding.name]),
        )
        self.assertEqual(list(manifest.parent.glob(".*.tmp")), [])

    def test_source_and_paired_overrides_do_not_touch_cache(self) -> None:
        spec = TARGET_SPECS["x86_64-unknown-linux-gnu"]
        for environ in (
            {"V8_FROM_SOURCE": "1"},
            {
                "RUSTY_V8_ARCHIVE": "archive.a",
                "RUSTY_V8_SRC_BINDING_PATH": "binding.rs",
            },
        ):
            with (
                self.subTest(environ=environ),
                patch.object(
                    v8,
                    "fetch_codex_v8_artifacts",
                    side_effect=AssertionError("unexpected fetch"),
                ) as fetch,
            ):
                self.assertEqual(
                    v8.resolve_codex_v8_cargo_env(spec, environ=environ), {}
                )
                fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()

import base64
import hashlib
import html
import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('fixcopy', ROOT / 'fixcopy.py')
fixcopy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fixcopy)


class InjectorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.js = self.root / 'stub.js'
        self.js.write_bytes(b'window.sample = true;\r\n')
        self.page = self.root / 'page.html'
        self.page.write_bytes(b'<body>Sample\r\n</BODY>')

    def cli(self, *args):
        return subprocess.run([sys.executable, str(ROOT / 'fixcopy.py'), *map(str, args)], capture_output=True, text=True)

    def inject(self, *args):
        return self.cli('inject', *args, '--script', self.js)

    def test_preflight_leaves_all_inputs_untouched(self):
        source = self.root / 'site'
        source.mkdir()
        good = source / 'a.html'
        original = b'<meta name="fixcopy">Good'
        good.write_bytes(original)
        (source / 'z.html').write_bytes(b'\xff')
        result = self.inject(source, '--in-place')
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(good.read_bytes(), original)
        out = self.root / 'copy'
        self.assertEqual(self.inject(source, '-o', out).returncode, 2)
        self.assertFalse(out.exists())

    def test_atomic_replace_and_staging_failure(self):
        other = self.root / 'other.html'
        other.write_bytes(b'Other')
        before = self.page.read_bytes()
        real_replace = fixcopy.os.replace
        replacements = []

        def replace(source, target):
            self.assertEqual(Path(source).parent, Path(target).parent)
            replacements.append(target)
            return real_replace(source, target)

        with mock.patch.object(fixcopy.os, 'replace', side_effect=replace):
            self.assertEqual(fixcopy.main(['inject', str(self.page), '--in-place', '--script', str(self.js)]), 0)
        self.assertEqual(replacements, [self.page])
        self.page.write_bytes(before)
        real_temp = fixcopy.tempfile.NamedTemporaryFile
        calls = 0

        def stage(**kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError('staging failed')
            return real_temp(**kwargs)

        with mock.patch.object(fixcopy.tempfile, 'NamedTemporaryFile', side_effect=stage):
            self.assertEqual(fixcopy.main(['inject', str(self.page), str(other), '--in-place', '--script', str(self.js)]), 2)
        self.assertEqual(self.page.read_bytes(), before)
        self.assertEqual(other.read_bytes(), b'Other')
        self.assertEqual(list(self.root.glob('.fixcopy-*')), [])

    def test_new_outputs_keep_source_mode(self):
        self.page.chmod(0o644)
        out = self.root / 'out.html'
        self.assertEqual(self.inject(self.page, '-o', out).returncode, 0)
        self.assertEqual(out.stat().st_mode & 0o777, 0o644)
        site = self.root / 'site'
        site.mkdir()
        (site / 'a.html').write_bytes(b'<meta name="fixcopy">A')
        (site / 'a.html').chmod(0o664)
        (site / 'style.css').write_bytes(b'p{}')
        (site / 'style.css').chmod(0o640)
        mirror = self.root / 'mirror'
        self.assertEqual(self.inject(site, '-o', mirror).returncode, 0)
        self.assertEqual((mirror / 'a.html').stat().st_mode & 0o777, 0o664)
        self.assertEqual((mirror / 'style.css').stat().st_mode & 0o777, 0o640)

    def test_special_mode_bits_are_not_copied(self):
        self.page.chmod(0o2755)
        out = self.root / 'out.html'
        self.assertEqual(self.inject(self.page, '-o', out).returncode, 0)
        self.assertEqual(out.stat().st_mode & 0o7777, 0o755)

    def test_symlinked_ancestor_is_allowed(self):
        real = self.root / 'real'
        real.mkdir()
        (real / 'page.html').write_bytes(b'<body>Sample</body>')
        link = self.root / 'link'
        link.symlink_to(real, target_is_directory=True)
        result = self.inject(link / 'page.html', '--in-place')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(b'data-fixcopy', (real / 'page.html').read_bytes())

    def test_no_status_lines_when_nothing_is_written(self):
        site = self.root / 'site'
        site.mkdir()
        (site / 'a.html').write_bytes(b'<meta name="fixcopy">A')
        (site / 'z.html').write_bytes(b'\xff')
        result = self.inject(site, '--in-place')
        self.assertEqual(result.returncode, 2)
        self.assertNotIn('injected', result.stdout)

    def test_check_without_output(self):
        original = self.page.read_bytes()
        self.assertEqual(self.inject(self.page, '--check').returncode, 1)
        self.assertEqual(self.page.read_bytes(), original)
        source = self.root / 'site'
        source.mkdir()
        page = source / 'a.html'
        page.write_text('<meta name="fixcopy">')
        before = page.read_bytes()
        self.assertEqual(self.inject(source, '--check').returncode, 1)
        self.assertEqual(page.read_bytes(), before)
        self.assertEqual(list(source.iterdir()), [page])

    def test_existing_outputs(self):
        out = self.root / 'out.html'
        out.write_text('Old')
        self.assertEqual(self.inject(self.page, '-o', out).returncode, 0)
        self.assertIn(b'data-fixcopy', out.read_bytes())
        source = self.root / 'site'
        source.mkdir()
        (source / 'a.html').write_text('<meta name="fixcopy">')
        destination = self.root / 'copy'
        destination.mkdir()
        keep = destination / 'keep'
        keep.write_bytes(b'Keep')
        result = self.inject(source, '-o', destination)
        self.assertEqual(result.returncode, 2)
        self.assertIn('choose a new directory', result.stderr)
        self.assertEqual(list(destination.iterdir()), [keep])
        keep.unlink()
        self.assertEqual(self.inject(source, '-o', destination).returncode, 0)

    def test_symlinks(self):
        link = self.root / 'link.html'
        link.symlink_to(self.page)
        original = self.page.read_bytes()
        self.assertEqual(self.inject(link, '--in-place').returncode, 2)
        self.assertEqual(self.inject(self.page, '-o', link).returncode, 2)
        self.assertEqual(self.page.read_bytes(), original)
        source = self.root / 'site'
        source.mkdir()
        (source / 'file.html').symlink_to(self.page)
        (source / 'folder').symlink_to(self.root, target_is_directory=True)
        out = self.root / 'copy'
        result = self.inject(source, '-o', out)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.count('skipped (symlink)'), 2)
        self.assertEqual(list(out.iterdir()), [])
        link.unlink()
        link.symlink_to(self.root / 'missing')
        self.assertEqual(self.inject(self.page, '-o', link).returncode, 2)

    def test_csp_content_span_preserved(self):
        for quote in ("'", '"', ''):
            policy = 'script-src https://example.test?a=1&amp;b=2' if quote else 'script-src'
            prefix = '<META id="csp" CONTENT='
            suffix = ' HTTP-EQUIV="Content-Security-Policy" data-extra="keep">'
            source = prefix + quote + policy + quote + suffix
            result = fixcopy.inject_text(source, 'x')
            rewritten = fixcopy.edit_policy(html.unescape(policy), fixcopy.script_hash('x'))
            used = quote or '"'
            escaped = rewritten.replace('&', '&amp;').replace(used, '&#39;' if used == "'" else '&quot;')
            self.assertTrue(result.startswith(prefix + used + escaped + used + suffix), result)

    def test_old_hash_scoped_to_receiving_directive(self):
        old = fixcopy.script_hash('old')
        for policy in (f"script-src 'unsafe-inline' {old}; style-src {old}",
                       f"default-src {old}; style-src {old}",
                       f"script-src {old}; script-src-elem 'self'; style-src {old}"):
            source = f'<meta http-equiv="Content-Security-Policy" content="{policy}"><script data-fixcopy>old</script>'
            result = fixcopy.Document(fixcopy.inject_text(source, 'new')).policies[0][2]
            self.assertIn('style-src ' + old, result)
            self.assertIn(fixcopy.script_hash('new'), result)
            if "'unsafe-inline'" in policy:
                self.assertIn("script-src 'unsafe-inline' " + fixcopy.script_hash('new'), result)
            if policy.startswith('default-src'):
                self.assertIn('default-src ' + old, result)

    def test_unterminated_owned_script_names_file(self):
        self.page.write_text('<script data-fixcopy>old')
        before = self.page.read_bytes()
        result = self.inject(self.page, '--in-place')
        self.assertEqual(result.returncode, 2)
        self.assertIn(str(self.page), result.stderr)
        self.assertEqual(self.page.read_bytes(), before)

    def test_comment_opener_script_rejected(self):
        self.js.write_text('<!--<script>')
        self.assertEqual(self.inject(self.page, '--in-place').returncode, 2)

    def test_help_documents_arguments(self):
        result = self.cli('inject', '--help')
        for phrase in ('<meta name="fixcopy">', '--all', 'named file', 'next to fixcopy.py', 'overwrites'):
            self.assertIn(phrase, ' '.join(result.stdout.split()))
        parser_help = self.cli('hash', '--help').stdout
        self.assertIn('next to fixcopy.py', parser_help)

    def test_system_temp_location(self):
        self.assertNotIn(ROOT, self.root.parents)

    def test_policy_cases(self):
        cases = [("default-src 'none'", True), ("script-src 'unsafe-inline'", False),
                 ("default-src 'unsafe-inline'", False), ("img-src 'self'", False),
                 ("script-src 'unsafe-inline' 'nonce-abc'", True),
                 ("script-src 'unsafe-inline' 'sha256-old'", True),
                 ("script-src 'none'; script-src-elem 'self'", True),
                 ("default-src 'self' 'none'", True),
                 ("script-src 'none'; script-src-elem 'unsafe-inline'", False),
                 ("script-src 'unsafe-inline' 'strict-dynamic'", True),
                 ("script-src 'UNSAFE-INLINE'", False), ("script-src 'NONE'", True)]
        for policy, changed in cases:
            with self.subTest(policy=policy):
                meta = f'<META content="{policy}" HTTP-EQUIV="Content-Security-Policy">'
                result = fixcopy.inject_text(meta, 'sample();')
                self.assertEqual(meta not in result, changed)
                if changed:
                    self.assertIn(fixcopy.script_hash('sample();'), html.unescape(result))
        result = fixcopy.inject_text('<meta http-equiv="Content-Security-Policy" content="script-src &apos;none&apos;">' * 2, 'x')
        self.assertEqual(html.unescape(result).count(fixcopy.script_hash('x')), 2)

    def test_injection_and_update(self):
        source = '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'"><body>Text\r\n<script src="assets/fixcopy.js?v=1"></script></BODY>'
        first = fixcopy.inject_text(source, 'first\r\n')
        self.assertIn('<script data-fixcopy>first\n</script></BODY>', first)
        self.assertNotIn('src=', first)
        self.assertIn('Text\r\n', first)
        self.assertEqual(first, fixcopy.inject_text(first, 'first\n'))
        updated = fixcopy.inject_text(first, 'second')
        self.assertNotIn(fixcopy.script_hash('first\n'), html.unescape(updated))
        self.assertIn(fixcopy.script_hash('second'), html.unescape(updated))
        self.assertEqual(updated.count('data-fixcopy'), 1)
        self.assertTrue(fixcopy.inject_text('text', 'x').endswith('<script data-fixcopy>x</script>'))

    def test_unsafe_inline_hash_update(self):
        source = "<meta http-equiv=\"Content-Security-Policy\" content=\"script-src 'unsafe-inline' 'sha256-other'\">"
        first = fixcopy.inject_text(source, 'x')
        # Removing an obsolete third-party hash must not make repeat runs unstable.
        first = first.replace(" " + html.escape("'sha256-other'", quote=True), '')
        self.assertEqual(first, fixcopy.inject_text(first, 'x'))
        updated = html.unescape(fixcopy.inject_text(first, 'y'))
        self.assertIn(fixcopy.script_hash('y'), updated)
        self.assertNotIn(fixcopy.script_hash('x'), updated)

    def test_false_markup(self):
        tag = '<meta name="fixcopy">'
        for wrapper in ('<!--{}-->', '<script>{}</script>', '<style>{}</style>', '<textarea>{}</textarea>', '<title>{}</title>'):
            text = wrapper.format(tag)
            self.assertFalse(fixcopy.Document(text).tagged)
        source = '<script>const x="</body>";</script><!-- </body> --><textarea></body></textarea>'
        self.assertEqual(fixcopy.inject_text(source, 'x'), source + '<script data-fixcopy>x</script>')
        self.assertTrue(fixcopy.Document('<META NAME="FIXCOPY"/>').tagged)

    def test_file_output_check_and_in_place(self):
        self.assertNotEqual(self.inject(self.page).returncode, 0)
        self.assertNotEqual(self.inject(self.page, '-o', self.page).returncode, 0)
        out = self.root / 'out.html'
        result = self.inject(self.page, '-o', out, '--check')
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertFalse(out.exists())
        self.assertIn(str(self.page), result.stdout)
        self.assertEqual(self.inject(self.page, '-o', out).returncode, 0)
        self.assertIn(b'data-fixcopy', out.read_bytes())
        self.assertEqual(self.inject(out, '--in-place', '--check').returncode, 0)
        self.assertEqual(self.inject(self.page, '--in-place').returncode, 0)
        self.assertIn(b'data-fixcopy', self.page.read_bytes())

    def test_directory(self):
        source = self.root / 'site'
        source.mkdir()
        (source / 'sub').mkdir()
        tagged = source / 'sub' / 'tag.htm'
        tagged.write_text('<meta name="fixcopy"><body>Text</body>')
        plain = source / 'plain.html'
        plain.write_bytes(b'<body>Plain\r\n</body>')
        asset = source / 'asset.bin'
        asset.write_bytes(b'\xff\x00\r\n')
        out = self.root / 'copy'
        self.assertNotEqual(self.inject(source, '-o', source / 'nested').returncode, 0)
        result = self.inject(source, '-o', out)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('skipped (no fixcopy meta tag)', result.stdout)
        self.assertIn(b'data-fixcopy', (out / 'sub/tag.htm').read_bytes())
        self.assertEqual((out / 'plain.html').read_bytes(), plain.read_bytes())
        self.assertEqual((out / 'asset.bin').read_bytes(), asset.read_bytes())
        self.assertEqual(self.inject(source, '--all', '--in-place').returncode, 0)
        self.assertIn(b'data-fixcopy', plain.read_bytes())

    def test_multiple_inputs_and_empty_directories(self):
        other = self.root / 'other.htm'
        other.write_text('Other')
        self.assertEqual(self.inject(self.page, other, '--in-place').returncode, 0)
        self.assertIn('data-fixcopy', other.read_text())
        self.assertNotEqual(self.inject(self.page, other, '-o', self.root / 'out').returncode, 0)
        source = self.root / 'empty'
        (source / 'child').mkdir(parents=True)
        out = self.root / 'mirror'
        self.assertEqual(self.inject(source, '-o', out).returncode, 0)
        self.assertTrue((out / 'child').is_dir())
        self.assertNotEqual(self.inject(self.root / 'missing', '--in-place').returncode, 0)

    def test_raw_text_and_policy_preservation(self):
        source = '<textarea><script><meta name="fixcopy"></textarea><meta name="fixcopy">'
        self.assertTrue(fixcopy.Document(source).tagged)
        untouched = "<META HTTP-EQUIV='Content-Security-Policy' content=\"script-src 'unsafe-inline'\">"
        result = fixcopy.inject_text(untouched, 'x')
        self.assertIn(untouched, result)
        self.assertEqual(result, fixcopy.inject_text(result, 'y').replace('>y</script>', '>x</script>'))
        source = '<script src="other.js"></script><body>One</body>Two</BODY>'
        self.assertIn('Two<script data-fixcopy>x</script></BODY>', fixcopy.inject_text(source, 'x'))
        self.assertIn('src="other.js"', fixcopy.inject_text(source, 'x'))

    def test_errors_and_hash(self):
        result = self.cli('hash', '--script', self.js)
        expected = "'sha256-" + base64.b64encode(hashlib.sha256(b'window.sample = true;\n').digest()).decode() + "'"
        self.assertEqual(result.stdout.strip(), expected)
        for data in (b'bad </ScRiPt>', b'\xff'):
            self.js.write_bytes(data)
            self.assertNotEqual(self.inject(self.page, '--in-place').returncode, 0)
        self.js.unlink()
        self.assertIn('script', self.inject(self.page, '--in-place').stderr)
        self.js.write_text('x')
        self.page.write_bytes(b'\xff')
        result = self.inject(self.page, '--in-place')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(str(self.page), result.stderr)
        self.assertEqual(self.cli('--version').stdout.strip(), '0.1.0')


if __name__ == '__main__':
    unittest.main()

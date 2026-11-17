import base64
import hashlib
import html
import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('fixcopy', ROOT / 'fixcopy.py')
fixcopy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fixcopy)


class InjectorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT)
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

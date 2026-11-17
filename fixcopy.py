#!/usr/bin/env python3
"""Inline fixcopy into UTF-8 HTML files and update their CSP meta tags."""
import argparse
import base64
import hashlib
import html
from html.parser import HTMLParser
from pathlib import Path
import re
import shutil
import sys
from urllib.parse import unquote, urlsplit


def normalize(js):
    js = js.replace('\r\n', '\n').replace('\r', '\n')
    if '</script' in js.lower():
        raise ValueError('script contains </script')
    return js


def script_hash(js):
    return "'sha256-" + base64.b64encode(hashlib.sha256(normalize(js).encode('utf-8')).digest()).decode('ascii') + "'"


class Document(HTMLParser):
    """Collect edit spans without reserializing unrelated markup."""
    CDATA_CONTENT_ELEMENTS = ('script', 'style', 'textarea', 'title')

    def __init__(self, text):
        super().__init__(convert_charrefs=False)
        self.text = text
        self.lines = [0] + [m.end() for m in re.finditer('\n', text)]
        self.tagged = False
        self.policies = []
        self.scripts = []
        self.bodies = []
        self.raw = None
        self.script = None
        self.feed(text)
        self.close()

    def position(self):
        line, column = self.getpos()
        return self.lines[line - 1] + column

    def handle_starttag(self, tag, attrs):
        if self.raw:
            return
        attrs = dict(attrs)
        start = self.position()
        if tag in ('textarea', 'title', 'style', 'script'):
            self.raw = tag
        if tag == 'script':
            src = attrs.get('src') or ''
            external = unquote(urlsplit(src).path).rsplit('/', 1)[-1] == 'fixcopy.js'
            self.script = (start, start + len(self.get_starttag_text()), 'data-fixcopy' in attrs, external)
        if tag == 'meta':
            self.tagged |= (attrs.get('name') or '').lower() == 'fixcopy'
            if (attrs.get('http-equiv') or '').lower() == 'content-security-policy':
                self.policies.append((start, start + len(self.get_starttag_text()), attrs.get('content') or ''))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if self.raw:
            if tag != self.raw:
                return
            if tag == 'script' and self.script:
                start, content_start, owned, external = self.script
                end_start = self.position()
                end = self.text.find('>', end_start) + 1
                if owned or external:
                    self.scripts.append((start, end, self.text[content_start:end_start] if owned else None))
                self.script = None
            self.raw = None
            return
        if tag == 'body':
            self.bodies.append(self.position())


def edit_policy(policy, token, old_tokens=()):
    directives = [part.split() for part in policy.split(';') if part.strip()]
    by_name = {}
    for directive in directives:
        by_name.setdefault(directive[0].lower(), directive)
    directive = by_name.get('script-src-elem', by_name.get('script-src'))
    fallback = directive is None
    if fallback:
        directive = by_name.get('default-src')
    if directive is None:
        return policy
    keywords = [source.lower() for source in directive[1:]]
    for part in directives:
        part[1:] = [source for source in part[1:] if source not in old_tokens]
    sources = directive[1:]
    if "'unsafe-inline'" in keywords and not any(re.match(r"'(?:sha256-|sha384-|sha512-|nonce-|strict-dynamic')", word) for word in keywords):
        if not old_tokens:
            return policy
    else:
        sources = [source for source in sources if source.lower() != "'none'"]
        if token not in sources:
            sources.append(token)
        if fallback:
            directives.append(['script-src', *sources])
        else:
            directive[1:] = sources
    result = '; '.join(' '.join(part) for part in directives)
    # Preserve original formatting when the policy already authorizes this script.
    if result == '; '.join(' '.join(part.split()) for part in policy.split(';') if part.strip()):
        return policy
    return result


def inject_text(text, js):
    """Return HTML with one inline script; input and output are Unicode strings."""
    js = normalize(js)
    doc = Document(text)
    old_tokens = {script_hash(content) for _, _, content in doc.scripts if content is not None}
    edits = [(start, end, '') for start, end, _ in doc.scripts]
    for start, end, policy in doc.policies:
        rewritten = edit_policy(policy, script_hash(js), old_tokens)
        if rewritten != policy:
            edits.append((start, end, '<meta http-equiv="content-security-policy" content="' + html.escape(rewritten, quote=True) + '">'))
    for start, end, replacement in sorted(edits, reverse=True):
        text = text[:start] + replacement + text[end:]
    doc = Document(text)
    end = doc.bodies[-1] if doc.bodies else len(text)
    return text[:end] + '<script data-fixcopy>' + js + '</script>' + text[end:]


def read_utf8(path):
    try:
        return path.read_bytes().decode('utf-8')
    except (OSError, UnicodeError) as error:
        raise ValueError(f'cannot read UTF-8 file {path}: {error}') from error


def main(argv=None):
    parser = argparse.ArgumentParser(description='Inline fixcopy and update CSP meta tags.')
    parser.add_argument('--version', action='version', version='0.1.0')
    commands = parser.add_subparsers(dest='command', required=True)
    inject = commands.add_parser('inject', help='inject HTML files', epilog='Example: fixcopy.py inject page.html --in-place')
    inject.add_argument('paths', nargs='+', type=Path)
    output = inject.add_mutually_exclusive_group(required=True)
    output.add_argument('-o', type=Path, metavar='OUT')
    output.add_argument('--in-place', action='store_true')
    inject.add_argument('--all', action='store_true', help='inject every HTML file in directories')
    inject.add_argument('--check', action='store_true', help='list changes without writing')
    hash_command = commands.add_parser('hash', help='print the CSP hash', epilog='Example: fixcopy.py hash --script fixcopy.js')
    for command in (inject, hash_command):
        command.add_argument('--script', type=Path, default=Path(__file__).resolve().with_name('fixcopy.js'))
    parser.epilog = 'Examples: fixcopy.py inject page.html --in-place; fixcopy.py hash'
    args = parser.parse_args(argv)
    try:
        try:
            js = normalize(read_utf8(args.script))
        except ValueError as error:
            raise ValueError(f'cannot load script: {error}') from error
        if args.command == 'hash':
            print(script_hash(js))
            return 0
        if args.o and len(args.paths) != 1:
            raise ValueError('-o requires exactly one input path')
        for source in args.paths:
            if not source.exists() or not (source.is_file() or source.is_dir()):
                raise ValueError(f'not a file or directory: {source}')
            if args.o:
                destination = args.o.resolve()
                origin = source.resolve()
                if destination == origin or (source.is_dir() and origin in destination.parents):
                    raise ValueError(f'output must be outside input: {args.o}')
        changed = False
        for source in args.paths:
            directory = source.is_dir()
            if directory and args.o and not args.check:
                args.o.mkdir(parents=True, exist_ok=True)
            files = sorted(source.rglob('*')) if directory else [source]
            for path in files:
                if path.is_dir() and args.o and not args.check:
                    (args.o / path.relative_to(source)).mkdir(parents=True, exist_ok=True)
                if not path.is_file():
                    continue
                target = (args.o / path.relative_to(source) if directory else args.o) if args.o else path
                is_html = not directory or path.suffix.lower() in ('.html', '.htm')
                data = path.read_bytes()
                result = data
                status = 'unchanged'
                if is_html:
                    text = read_utf8(path)
                    if directory and not args.all and not Document(text).tagged:
                        status = 'skipped (no fixcopy meta tag)'
                    else:
                        result = inject_text(text, js).encode('utf-8')
                        if result != data:
                            status = 'injected'
                            changed = True
                print(f'{path}: {status}')
                if not args.check and (target != path or result != data):
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if result == data:
                        shutil.copyfile(path, target)
                    else:
                        target.write_bytes(result)
        return 1 if args.check and changed else 0
    except (OSError, ValueError) as error:
        print(f'fixcopy: {error}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())

#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
    flask-07-upgrade
    ~~~~~~~~~~~~~~~~

    This command line script scans a whole application tree and attempts to
    output an unified diff with all the changes that are necessary to easily
    upgrade the application to 0.7 and to not yield deprecation warnings.

    This will also attempt to find `after_request` functions that don't modify
    the response and appear to be better suited for `teardown_request`.

    :copyright: (c) Copyright 2011 by Armin Ronacher.
    :license: see LICENSE for more details.
"""
import re
import os
import sys
import inspect
import difflib
import posixpath
from optparse import OptionParser

try:
    import ast
except ImportError:
    ast = None


_from_import_re = re.compile(r'^\s*from flask import\s+')
_direct_module_usage_re = re.compile(r'flask\.Module')
_string_re_part = r"('([^'\\]*(?:\\.[^'\\]*)*)'" \
                  r'|"([^"\\]*(?:\\.[^"\\]*)*)")'
_url_for_re = re.compile(r'\b(url_for\()(%s)' % _string_re_part)
_after_request_re = re.compile(r'((?:@\S+\.(?:app_)?))(after_request)(\b\s*$)(?m)')
_module_constructor_re = re.compile(r'Module\(__name__\s*(?:,\s*(%s))?' %
                                    _string_re_part)
_blueprint_related = [
    (re.compile(r'request\.module'), 'request.blueprint'),
    (re.compile(r'register_module'), 'register_blueprint')
]


def error(message):
    print >> sys.stderr, 'error:', message
    sys.exit(1)


def make_diff(filename, old, new):
    for line in difflib.unified_diff(old.splitlines(), new.splitlines(),
                     posixpath.normpath(posixpath.join('a', filename)),
                     posixpath.normpath(posixpath.join('b', filename)),
                     lineterm=''):
        print line


def fix_url_for(contents):
    def handle_match(match):
        prefix = match.group(1)
        endpoint = ast.literal_eval(match.group(2))
        if endpoint.startswith('.'):
            endpoint = endpoint[1:]
        else:
            endpoint = '.' + endpoint
        return prefix + repr(endpoint)
    return _url_for_re.sub(handle_match, contents)


def looks_like_teardown_function(node):
    returns = [x for x in ast.walk(node) if isinstance(x, ast.Return)]
    if len(returns) != 1:
        return
    return_def = returns[0]
    resp_name = node.args.args[0]
    if not isinstance(return_def.value, ast.Name) or \
       return_def.value.id != resp_name.id:
        return

    for body_node in node.body:
        for child in ast.walk(body_node):
            if isinstance(child, ast.Name) and \
               child.id == resp_name.id:
                if child is not return_def.value:
                    return

    return resp_name.id


def fix_teardown_funcs(contents):

    def is_return_line(line):
        args = line.strip().split()
        return args and args[0] == 'return'

    def fix_single(match, lines, lineno):
        if not lines[lineno + 1].startswith('def'):
            return
        block_lines = inspect.getblock(lines[lineno + 1:])
        func_code = ''.join(block_lines)
        if func_code[0].isspace():
            node = ast.parse('if 1:\n' + func_code).body[0].body
        else:
            node = ast.parse(func_code).body[0]
        response_param_name = looks_like_teardown_function(node)
        if response_param_name is None:
            return
        before = lines[:lineno]
        decorator = [match.group(1) +
                     match.group(2).replace('after_', 'teardown_') +
                     match.group(3)]
        body = [line.replace(response_param_name, 'exception')
                for line in block_lines if
                not is_return_line(line)]
        after = lines[lineno + len(block_lines) + 1:]
        return before + decorator + body + after

    content_lines = contents.splitlines(True)
    while 1:
        found_one = False
        for idx, line in enumerate(content_lines):
            match = _after_request_re.match(line)
            if match is None:
                continue
            new_content_lines = fix_single(match, content_lines, idx)
            if new_content_lines is not None:
                content_lines = new_content_lines
                break
        else:
            break

    return ''.join(content_lines)


def get_module_autoname(filename):
    directory, filename = os.path.split(filename)
    if filename != '__init__.py':
        return os.path.splitext(filename)[0]
    return os.path.basename(directory)


def rewrite_from_imports(prefix, fromlist, lineiter):
    import_block = [prefix, fromlist]
    if fromlist[0] == '(' and fromlist[-1] != ')':
        for line in lineiter:
            import_block.append(line)
            if line.rstrip().endswith(')'):
                break
    elif fromlist[-1] == '\\':
        for line in lineiter:
            import_block.append(line)
            if line.rstrip().endswith('\\'):
                break

    return ''.join(import_block).replace('Module', 'Blueprint')


def rewrite_blueprint_imports(contents):
    new_file = []
    lineiter = iter(contents.splitlines(True))
    for line in lineiter:
        match = _from_import_re.search(line)
        if match is not None:
            new_file.extend(rewrite_from_imports(match.group(),
                                                 line[match.end():],
                                                 lineiter))
            continue
        new_file.append(_direct_module_usage_re.sub('flask.Blueprint', line))
    return ''.join(new_file)


def rewrite_for_blueprints(contents, filename):
    found_constructor = []
    def handle_match(match):
        found_constructor[:] = [True]
        name_param = match.group(1)
        if name_param is None:
            return 'Blueprint(%r, __name__' % get_module_autoname(filename)
        return 'Blueprint(%s, __name__' % name_param
    new_contents = _module_constructor_re.sub(handle_match, contents)

    if found_constructor:
        new_contents = rewrite_blueprint_imports(new_contents)

    for pattern, replacement in _blueprint_related:
        new_contents = pattern.sub(replacement, new_contents)
    return new_contents


def upgrade_python_file(filename, contents, teardown):
    new_contents = fix_url_for(contents)
    if teardown:
        new_contents = fix_teardown_funcs(new_contents)
    new_contents = rewrite_for_blueprints(new_contents, filename)
    make_diff(filename, contents, new_contents)


def upgrade_template_file(filename, contents):
    new_contents = fix_url_for(contents)
    make_diff(filename, contents, new_contents)


def scan_path(path=None, teardown=True):
    for dirpath, dirnames, filenames in os.walk(path):
        for filename in filenames:
            filename = os.path.join(dirpath, filename)
            with open(filename) as f:
                contents = f.read()
            if filename.endswith('.py'):
                upgrade_python_file(filename, contents, teardown)
            elif '{% for' or '{% if' or '{{ url_for' in contents:
                upgrade_template_file(filename, contents)


def main():
    """Entrypoint"""
    if ast is None:
        error('Python 2.6 or later is required to run the upgrade script.\n'
              'The runtime requirements for Flask 0.7 however are still '
              'Python 2.5.')

    parser = OptionParser()
    parser.add_option('-T', '--no-teardown-detection', dest='no_teardown',
                      action='store_true', help='Do not attempt to '
                      'detect teardown function rewrites.')
    options, args = parser.parse_args()
    if not args:
        args = ['.']

    for path in args:
        scan_path(path, teardown=not options.no_teardown)


if __name__ == '__main__':
    main()

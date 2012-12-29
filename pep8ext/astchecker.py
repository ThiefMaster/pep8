"""Checkers on AST."""
import re
import sys
from collections import deque
try:
    import ast
    ast_iter_nodes = ast.iter_child_nodes
except ImportError:   # Python 2.5
    import _ast as ast

    def _ast_compat(node):
        if isinstance(node, ast.ClassDef):
            node.decorator_list = []
        elif isinstance(node, ast.FunctionDef):
            node.decorator_list = node.decorators
        elif node._fields is None:
            node._fields = ()
        return node

    def ast_iter_nodes(node, _ast_compat=_ast_compat):
        for name in _ast_compat(node)._fields:
            try:
                field = getattr(node, name)
            except AttributeError:
                continue
            if isinstance(field, ast.AST):
                yield _ast_compat(field)
            elif isinstance(field, list):
                for item in field:
                    if isinstance(item, ast.AST):
                        yield _ast_compat(item)

LOWERCASE_REGEX = re.compile(r'[_a-z][_a-z0-9]*$')
UPPERCASE_REGEX = re.compile(r'[_A-Z][_A-Z0-9]*$')
MIXEDCASE_REGEX = re.compile(r'_?[A-Z][a-zA-Z0-9]*$')


if sys.version_info[0] < 3:
    def get_arg_names(node):
        ret = []
        for arg in node.args.args:
            if isinstance(arg, ast.Tuple):
                for t_arg in arg.elts:
                    ret.append(t_arg.id)
            else:
                ret.append(arg.id)
        return ret
else:
    def get_arg_names(node):
        pos_args = [arg.arg for arg in node.args.args]
        kw_only = [arg.arg for arg in node.args.kwonlyargs]
        return pos_args + kw_only


def generate_ast(lines, filename, verbose=False):
    try:
        return compile(''.join(lines), '<unknown>', 'exec', ast.PyCF_ONLY_AST)
    except SyntaxError:
        if verbose:
            error = sys.exc_info()[1]
            print("Syntax error (%s) in file %s" % (error, filename))


class _ASTCheckMeta(type):
    def __init__(self, class_name, bases, namespace):
        try:
            self._checks.append(self())
        except AttributeError:
            self._checks = []


def _err(self, node, code):
    lineno, col_offset = node.lineno, node.col_offset
    if isinstance(node, ast.ClassDef):
        lineno += len(node.decorator_list)
        col_offset += 6
    elif isinstance(node, ast.FunctionDef):
        lineno += len(node.decorator_list)
        col_offset += 4
    return (lineno, col_offset, '%s %s' % (code, getattr(self, code)), self)
BaseASTCheck = _ASTCheckMeta('BaseASTCheck', (object,),
                             {'__doc__': "Base for AST Checks.", 'err': _err})


class ASTChecker(object):

    def __init__(self, tree, filename, options):
        self.visitors = BaseASTCheck._checks
        self.parents = deque()
        self._node = tree

    def run(self):
        return self._run(self._node) if self._node else ()

    def _run(self, node):
        for error in self.visit_node(node):
            yield error
        self.parents.append(node)
        for child in ast_iter_nodes(node):
            for error in self._run(child):
                yield error
        self.parents.pop()

    def visit_node(self, node):
        if isinstance(node, ast.ClassDef):
            self.tag_class_functions(node)
        elif isinstance(node, ast.FunctionDef):
            self.find_global_defs(node)

        method = 'visit_' + node.__class__.__name__.lower()
        for visitor in self.visitors:
            if not hasattr(visitor, method):
                continue
            for error in getattr(visitor, method)(node, self.parents):
                yield error

    def tag_class_functions(self, cls_node):
        """Tag functions if they are methods, classmethods, staticmethods"""
        # tries to find all 'old style decorators' like
        # m = staticmethod(m)
        late_decoration = {}
        for node in ast_iter_nodes(cls_node):
            if not (isinstance(node, ast.Assign) and
                    isinstance(node.value, ast.Call) and
                    isinstance(node.value.func, ast.Name)):
                continue
            func_name = node.value.func.id
            if func_name in ('classmethod', 'staticmethod'):
                meth = (len(node.value.args) == 1 and node.value.args[0])
                if isinstance(meth, ast.Name):
                    late_decoration[meth.id] = func_name

        # iterate over all functions and tag them
        for node in ast_iter_nodes(cls_node):
            if not isinstance(node, ast.FunctionDef):
                continue
            node.function_type = 'method'
            if node.name in late_decoration:
                node.function_type = late_decoration[node.name]
            elif node.decorator_list:
                names = [d.id for d in node.decorator_list
                         if isinstance(d, ast.Name) and
                         d.id in ('classmethod', 'staticmethod')]
                if names:
                    node.function_type = names[0]

    def find_global_defs(self, func_def_node):
        global_names = set()
        nodes_to_check = deque(ast_iter_nodes(func_def_node))
        while nodes_to_check:
            node = nodes_to_check.pop()
            if isinstance(node, ast.Global):
                global_names.update(node.names)

            if not isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                nodes_to_check.extend(ast_iter_nodes(node))
        func_def_node.global_names = global_names


class ClassNameCheck(BaseASTCheck):
    """
    Almost without exception, class names use the CapWords convention.

    Classes for internal use have a leading underscore in addition.
    """
    check = MIXEDCASE_REGEX.match
    E800 = "class names should use CapWords convention"

    def visit_classdef(self, node, parents):
        if not self.check(node.name):
            yield self.err(node, 'E800')


class FunctionNameCheck(BaseASTCheck):
    """
    Function names should be lowercase, with words separated by underscores
    as necessary to improve readability.
    Functions *not* beeing methods '__' in front and back are not allowed.

    mixedCase is allowed only in contexts where that's already the
    prevailing style (e.g. threading.py), to retain backwards compatibility.
    """
    check = LOWERCASE_REGEX.match
    E801 = "function name should be lowercase"

    def visit_functiondef(self, node, parents):
        function_type = getattr(node, 'function_type', 'function')
        name = node.name
        if ((function_type == 'function' and '__' in (name[:2], name[-2:])) or
                not self.check(name)):
            yield self.err(node, 'E801')


class FunctionArgNamesCheck(BaseASTCheck):
    """
    The argument names of a function should be lowercase, with words separated
    by underscores.

    A classmethod should have 'cls' as first argument.
    A method should have 'self' as first argument.
    """
    check = LOWERCASE_REGEX.match
    E802 = "argument name should be lowercase"
    E803 = "first argument of a classmethod should be named 'cls'"
    E804 = "first argument of a method should be named 'self'"

    def visit_functiondef(self, node, parents):
        if node.args.kwarg is not None:
            if not self.check(node.args.kwarg):
                yield self.err(node, 'E802')
                return

        if node.args.vararg is not None:
            if not self.check(node.args.vararg):
                yield self.err(node, 'E802')
                return

        arg_names = get_arg_names(node)
        if not arg_names:
            return
        function_type = getattr(node, 'function_type', 'function')

        if function_type == 'method':
            if arg_names[0] != 'self':
                yield self.err(node, 'E804')
        elif function_type == 'classmethod':
            if arg_names[0] != 'cls':
                yield self.err(node, 'E803')
        for arg in arg_names:
            if not self.check(arg):
                yield self.err(node, 'E802')
                return


class ImportAsCheck(BaseASTCheck):
    """
    Don't change the naming convention via an import
    """
    check_lower = LOWERCASE_REGEX.match
    check_upper = UPPERCASE_REGEX.match
    W800 = "constant imported as non constant"
    W801 = "lowercase imported as non lowercase"
    W802 = "camelcase imported as lowercase"
    W803 = "camelcase imported as constant"

    def visit_importfrom(self, node, parents):
        for name in node.names:
            if not name.asname:
                continue
            if self.check_upper(name.name):
                if not self.check_upper(name.asname):
                    yield self.err(node, 'W800')
            elif self.check_lower(name.name):
                if not self.check_lower(name.asname):
                    yield self.err(node, 'W801')
            elif self.check_lower(name.asname):
                yield self.err(node, 'W802')
            elif self.check_upper(name.asname):
                yield self.err(node, 'W803')


class VariablesInFunctionCheck(BaseASTCheck):
    """
    Local variables in functions should be lowercase
    """
    check = LOWERCASE_REGEX.match
    E805 = "variable in function should be lowercase"

    def visit_assign(self, node, parents):
        for parent_func in reversed(parents):
            if isinstance(parent_func, ast.ClassDef):
                return
            if isinstance(parent_func, ast.FunctionDef):
                break
        else:
            return
        for target in node.targets:
            name = isinstance(target, ast.Name) and target.id
            if not name or name in parent_func.global_names:
                return
            if not self.check(name) and name[:1] != '_':
                yield self.err(target, 'E805')

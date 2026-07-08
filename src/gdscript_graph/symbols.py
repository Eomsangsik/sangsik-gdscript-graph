from __future__ import annotations

from dataclasses import dataclass, field

from lark import Token, Tree

from gdscript_graph.parsing import ParseResult


@dataclass
class FunctionSymbol:
    name: str
    line: int
    is_static: bool
    scope: str | None = None


@dataclass
class SignalSymbol:
    name: str
    line: int
    scope: str | None = None


@dataclass
class FieldSymbol:
    name: str
    line: int
    kind: str  # "var" | "const"
    scope: str | None = None


@dataclass
class EnumSymbol:
    name: str
    line: int
    scope: str | None = None


@dataclass
class FileSymbols:
    res_path: str
    class_name: str | None
    extends: str | None
    functions: list[FunctionSymbol]
    signals: list[SignalSymbol]
    fields: list[FieldSymbol] = field(default_factory=list)
    enums: list[EnumSymbol] = field(default_factory=list)


@dataclass
class FuncDefInfo:
    scope: str | None  # dotted enclosing class_def path, e.g. "Outer.Inner"
    name: str
    line: int
    is_static: bool
    node: Tree  # the func_def node (unwrapped from static_func_def, if any)


def first_name_token(tree: Tree) -> str | None:
    for child in tree.children:
        if isinstance(child, Token) and child.type == "NAME":
            return str(child)
    return None


def extract_class_name(tree: Tree) -> str | None:
    for child in tree.children:
        if isinstance(child, Tree) and child.data in ("classname_stmt", "classname_extends_stmt"):
            return first_name_token(child)
    return None


def extract_extends(tree: Tree) -> str | None:
    """Return the declared parent as a class name (or dotted path, e.g.
    ``Base.State`` for ``extends Base.State``), or a res:// path for
    string-based extends (e.g. ``extends "res://base.gd"``).

    A dotted parent path can never match a project's (always single-name)
    declared ``class_name``, so `build_inheritance_map` naturally treats it
    as an untracked/unknown parent rather than something that needs
    truncating here -- keeping only the first segment would misattribute
    the parent to an unrelated top-level class of that name instead of
    honestly leaving it unresolved (extending a *nested* class isn't
    tracked in v1, same as inner classes' own extends).

    Only scans the file's top-level statements (direct children of the
    root), not the whole tree -- a nested `class Inner: extends ...` has
    its own, separately-scoped parent that must not be mistaken for the
    file's own top-level extends.
    """
    for child in tree.children:
        if not (isinstance(child, Tree) and child.data in ("extends_stmt", "classname_extends_stmt")):
            continue
        for grandchild in child.children:
            if isinstance(grandchild, Tree) and grandchild.data == "string":
                string_token = next(
                    (c for c in grandchild.children if isinstance(c, Token)), None
                )
                if string_token is not None:
                    return str(string_token).strip("\"'")
        names = [str(c) for c in child.children if isinstance(c, Token) and c.type == "NAME"]
        if child.data == "extends_stmt":
            if names:
                return ".".join(names)
        elif len(names) >= 2:
            return ".".join(names[1:])
    return None


def _unwrap_func_def(node: Tree) -> tuple[Tree | None, bool]:
    """Return (func_def node, is_static) for a func_def or static_func_def node."""
    if node.data == "static_func_def":
        inner = node.children[0] if node.children else None
        if isinstance(inner, Tree) and inner.data == "func_def":
            return inner, True
        return None, True
    return node, False


def iter_scoped_subtrees(tree: Tree) -> list[tuple[str | None, Tree]]:
    """Walk the tree once, yielding every subtree paired with its enclosing
    class_def scope (None for top-level). Shared scope-tracking so
    signal/field/enum extraction agrees with iter_function_defs about which
    declarations belong to which scope -- without it, a signal/field/enum
    declared inside an inner class would collide with a same-named one at
    top level."""
    results: list[tuple[str | None, Tree]] = []
    scope_stack: list[str] = []

    def walk(node: object) -> None:
        if not isinstance(node, Tree):
            return
        results.append((".".join(scope_stack) if scope_stack else None, node))
        if node.data == "class_def":
            class_name = first_name_token(node)
            if class_name is not None:
                scope_stack.append(class_name)
            for child in node.children:
                walk(child)
            if class_name is not None:
                scope_stack.pop()
            return
        if node.data in ("func_def", "static_func_def", "property_body_def", "lambda"):
            # Don't descend into function bodies, inline property accessor
            # (`set`/`get`) bodies, or a lambda's body (e.g. a class-level
            # `var handler = func(): ...` initializer): a class can't be
            # nested inside any of these, and while signal/enum/var
            # declarations can't appear there either, `const` shares its
            # grammar node (const_stmt) with function-/accessor-/
            # lambda-local consts -- without this guard a local const would
            # be misattributed as a class field.
            return
        for child in node.children:
            walk(child)

    walk(tree)
    return results


def iter_function_defs(tree: Tree) -> list[FuncDefInfo]:
    """Walk the tree once, yielding every function with its enclosing
    class_def scope (None for top-level). Centralizes the scope-tracking
    logic shared by symbol extraction and call extraction so both agree on
    which functions are distinct symbols."""
    results: list[FuncDefInfo] = []
    scope_stack: list[str] = []

    def walk(node: object) -> None:
        if not isinstance(node, Tree):
            return

        if node.data == "class_def":
            class_name = first_name_token(node)
            if class_name is not None:
                scope_stack.append(class_name)
            for child in node.children:
                walk(child)
            if class_name is not None:
                scope_stack.pop()
            return

        if node.data in ("static_func_def", "func_def", "abstract_func_def"):
            # abstract_func_def (`func foo(x):` with no body/colon -- an
            # abstract method declaration) has no _func_suite: func_def_node
            # ends up with only its header as a child, so there's nothing
            # to walk for calls, but the declaration itself is still a real
            # symbol that other code can call.
            func_def_node, is_static = _unwrap_func_def(node)
            if func_def_node is None:
                return
            header = func_def_node.children[0] if func_def_node.children else None
            if isinstance(header, Tree) and header.data in ("func_header", "abstract_func_header"):
                name = first_name_token(header)
                if name is not None:
                    line = getattr(header.meta, "line", 0)
                    scope = ".".join(scope_stack) if scope_stack else None
                    results.append(FuncDefInfo(scope, name, line, is_static, func_def_node))
            return

        if node.data in ("property_body_def", "lambda"):
            # Neither can contain a nested named func/static/abstract func
            # def (only plain statements/expressions, or -- for lambda --
            # further nested lambdas, which this walk doesn't track
            # anyway), so there's nothing here for this walk to find.
            # Stopping also avoids needless deep recursion into either
            # one's body -- mirrors the identical stop-list already used
            # by iter_scoped_subtrees for the same reason.
            return

        for child in node.children:
            walk(child)

    walk(tree)
    return results


@dataclass
class PropertyAccessorInfo:
    scope: str | None
    name: str  # synthetic, e.g. "health.set" / "health.get" -- can't
               # collide with a real declared function (NAME tokens can't
               # contain ".")
    line: int
    body: list[Tree]
    param_name: str | None = None  # the setter's implicit value parameter; None for a getter


def iter_property_accessor_defs(tree: Tree) -> list[PropertyAccessorInfo]:
    """Walk the tree once, yielding the `set(value): ...` / `get: ...`
    bodies of inline property accessors (``var x: T = default: set(v): ...
    get: ...``), paired with a synthetic name derived from the property
    they belong to.

    These accessor bodies aren't `func_def` nodes -- `iter_function_defs`
    never sees them -- so without this, any call made inside one (e.g. a
    setter calling another method) would be silently absent from the call
    graph entirely, not even recorded as unresolved. A `property_body_def`
    is always the sibling immediately following the `class_var_stmt` it
    belongs to, so the property name is tracked as we walk each block of
    sibling statements."""
    results: list[PropertyAccessorInfo] = []

    def walk_block(children: list, scope: str | None) -> None:
        pending_var_name: str | None = None
        for child in children:
            if not isinstance(child, Tree):
                continue
            if child.data in ("class_var_stmt", "static_class_var_stmt"):
                # `static var x: T = v:` wraps its class_var_stmt one level
                # deeper (static_class_var_stmt -> class_var_stmt), but is
                # still the property_body_def's preceding sibling either way.
                var_stmt = child
                if child.data == "static_class_var_stmt" and child.children:
                    inner = child.children[0]
                    var_stmt = inner if isinstance(inner, Tree) else child
                pending_var_name = _first_name_token_deep(var_stmt)
                continue
            if child.data == "property_body_def":
                if pending_var_name is not None:
                    for accessor in child.children:
                        if not isinstance(accessor, Tree):
                            continue
                        param_name: str | None = None
                        if accessor.data == "property_custom_setter":
                            body_start = 2  # skip SET, value-param NAME
                            suffix = "set"
                            if len(accessor.children) > 1 and isinstance(accessor.children[1], Token):
                                param_name = str(accessor.children[1])
                        elif accessor.data == "property_custom_getter":
                            body_start = 1  # skip GET
                            suffix = "get"
                        else:
                            continue
                        line = getattr(accessor.meta, "line", 0)
                        body = [c for c in accessor.children[body_start:] if isinstance(c, Tree)]
                        results.append(PropertyAccessorInfo(
                            scope=scope, name=f"{pending_var_name}.{suffix}", line=line, body=body,
                            param_name=param_name,
                        ))
                pending_var_name = None
                continue
            if child.data == "annotation":
                # An annotation (e.g. `@export`) modifies whatever
                # statement follows it -- it must not be treated as an
                # intervening statement that severs the pending
                # class_var_stmt -> property_body_def association, even
                # though gdtoolkit's grammar allows one to appear between
                # them (an unusual shape real Godot likely never writes,
                # but silently dropping the accessor entirely if it does
                # occur is worse than just skipping past the annotation).
                continue
            pending_var_name = None
            if child.data == "class_def":
                class_name = first_name_token(child)
                inner_scope = ".".join(filter(None, [scope, class_name])) if class_name else scope
                walk_block(child.children, inner_scope)

    walk_block(tree.children, None)
    return results


def _collect_header_arg_types(header: Tree, types: dict[str, str | None]) -> None:
    """Walk a `func_header` node for its own declared parameters, e.g.
    `func_arg_typed` wrapped inside `func_arg_variadic`. Stops at a nested
    `lambda` node's boundary -- a lambda embedded in a default-argument-value
    expression (`func f(x = call(func(y): ...)):`) has its own, separate
    parameter list, which must not leak into the enclosing function's own
    parameter types (a plain unconditional `iter_subtrees()` walk would find
    the lambda's `func_arg_*` nodes too, indistinguishable from the
    enclosing function's real parameters)."""

    def walk(node: object) -> None:
        if not isinstance(node, Tree) or node.data == "lambda":
            return
        if node.data == "func_arg_typed" and len(node.children) >= 2:
            name, type_hint = node.children[0], node.children[1]
            if isinstance(name, Token) and isinstance(type_hint, Token):
                types[str(name)] = str(type_hint)
        elif node.data in ("func_arg_regular", "func_arg_inf") and node.children:
            name = node.children[0]
            if isinstance(name, Token):
                types.setdefault(str(name), None)
        for child in node.children:
            walk(child)

    walk(header)


def _collect_header_arg_names(header: Tree, names: set[str]) -> None:
    """Like `_collect_header_arg_types`, but for `extract_lambda_shadowed_names`'s
    need to know only a lambda's own parameter *names* (not their types) --
    same nested-`lambda`-boundary rule applies: a lambda embedded in this
    lambda's own default-argument-value expression has its own, separate
    parameter list that must not be attributed to this lambda."""

    def walk(node: object) -> None:
        if not isinstance(node, Tree) or node.data == "lambda":
            return
        if node.data in _LAMBDA_ARG_NODES and node.children:
            name = node.children[0]
            if isinstance(name, Token):
                names.add(str(name))
        for child in node.children:
            walk(child)

    walk(header)


def _collect_local_var_types(body: list, types: dict[str, str | None]) -> None:
    """Shared walk for `extract_local_var_types` and the property-accessor
    equivalent below -- both need identical var/for-loop-variable
    collection over a list of body statements, differing only in how their
    header/implicit parameters are seeded beforehand."""

    def walk(node: object) -> None:
        if not isinstance(node, Tree) or node.data == "lambda":
            return
        if node.data in ("func_var_typed", "func_var_typed_assgnd", "const_typed_assigned") and len(node.children) >= 2:
            name, type_hint = node.children[0], node.children[1]
            if isinstance(name, Token) and isinstance(type_hint, Token):
                types[str(name)] = str(type_hint)
            return
        if (
            node.data
            in ("func_var_empty", "func_var_assigned", "func_var_inf", "const_assigned", "const_inf", "var_capture_pattern")
            and node.children
        ):
            name = node.children[0]
            if isinstance(name, Token):
                types[str(name)] = None
            return
        if node.data == "for_stmt_typed" and len(node.children) >= 2:
            name, type_hint = node.children[0], node.children[1]
            if isinstance(name, Token) and isinstance(type_hint, Token):
                types[str(name)] = str(type_hint)
        elif node.data == "for_stmt" and node.children:
            name = node.children[0]
            if isinstance(name, Token):
                types[str(name)] = None
        for child in node.children:
            walk(child)

    for child in body:
        walk(child)


def extract_local_var_types(func_def_node: Tree) -> dict[str, str | None]:
    """Map every local var/param/for-loop-variable name declared in a single
    function to its declared type name, or ``None`` if it has no explicit
    type annotation (no inference from assignment, e.g. ``var x =
    Foo.new()`` maps to ``None`` too). Every local name is recorded, not
    just typed ones, so callers can tell "this name is a known local --
    shadowing any same-named autoload/class_name -- but of unresolvable
    type" apart from "this name isn't a local at all". Nested lambda
    bodies are skipped: they're their own scope, and walking into them
    would let a lambda-local variable clobber a same-named variable in the
    enclosing function's map."""
    types: dict[str, str | None] = {}

    header = func_def_node.children[0] if func_def_node.children else None
    if isinstance(header, Tree):
        _collect_header_arg_types(header, types)

    _collect_local_var_types(func_def_node.children[1:], types)
    return types


def extract_property_accessor_local_var_types(pa: "PropertyAccessorInfo") -> dict[str, str | None]:
    """Same as `extract_local_var_types`, but for a property accessor body
    (``set``/``get``) rather than a `func_def` -- without this, a setter's
    implicit value parameter (and any locals it declares) would be
    invisible to the "a local shadows a same-named autoload/class_name"
    rule, since `iter_function_defs` never sees accessor bodies at all."""
    types: dict[str, str | None] = {}
    if pa.param_name is not None:
        types[pa.param_name] = None
    _collect_local_var_types(pa.body, types)
    return types


_LAMBDA_ARG_NODES = ("func_arg_regular", "func_arg_typed", "func_arg_inf")
_LOCAL_VAR_DECL_NODES = (
    "func_var_empty", "func_var_assigned", "func_var_typed", "func_var_typed_assgnd", "func_var_inf",
    "const_assigned", "const_typed_assigned", "const_inf", "var_capture_pattern",
    "for_stmt", "for_stmt_typed",
)


def _collect_lambda_shadowed_names(body: list, names: set[str]) -> None:
    """Shared walk for `extract_lambda_shadowed_names` and the
    property-accessor equivalent below."""

    def walk(node: object, in_lambda: bool) -> None:
        if not isinstance(node, Tree):
            return
        if node.data == "lambda":
            header = node.children[0] if node.children else None
            if isinstance(header, Tree):
                _collect_header_arg_names(header, names)
            for child in node.children[1:]:
                walk(child, True)
            return
        if in_lambda and node.data in _LOCAL_VAR_DECL_NODES and node.children:
            name = node.children[0]
            if isinstance(name, Token):
                names.add(str(name))
        for child in node.children:
            walk(child, in_lambda)

    for child in body:
        walk(child, False)


def extract_lambda_shadowed_names(func_def_node: Tree) -> set[str]:
    """Names declared (as a var or parameter) inside any lambda nested in
    this function, at any depth.

    A call made *inside* a lambda on one of these names can't safely use
    the enclosing function's local_var_types: the lambda may re-declare
    the same name with a different (or no) type, and since
    `extract_local_var_types` deliberately doesn't see into lambda bodies,
    the enclosing function's type for that name -- if any -- would be
    stale for calls made after the lambda's own declaration. Trusting it
    anyway would silently misattribute the call instead of honestly
    leaving it unresolved.

    Also scans the header, not just the body: a lambda embedded in a
    default-argument-value expression (`func f(x, cb = func(): ...)`) is
    nested in this function just as much as one declared in the body --
    `calls.py` already walks the header and marks calls inside such a
    lambda `in_lambda=True`, so this must find its declarations too."""
    names: set[str] = set()
    header = func_def_node.children[0] if func_def_node.children else None
    if isinstance(header, Tree):
        _collect_lambda_shadowed_names([header], names)
    _collect_lambda_shadowed_names(func_def_node.children[1:], names)
    return names


def extract_property_accessor_lambda_shadowed_names(pa: "PropertyAccessorInfo") -> set[str]:
    """Same as `extract_lambda_shadowed_names`, but for a property accessor
    body (``set``/``get``) rather than a `func_def`."""
    names: set[str] = set()
    _collect_lambda_shadowed_names(pa.body, names)
    return names


def extract_functions(tree: Tree) -> list[FunctionSymbol]:
    return [
        FunctionSymbol(name=fd.name, line=fd.line, is_static=fd.is_static, scope=fd.scope)
        for fd in iter_function_defs(tree)
    ]


def extract_property_accessors(tree: Tree) -> list[FunctionSymbol]:
    """Property setter/getter bodies as synthetic function symbols (see
    `iter_property_accessor_defs`), so calls made inside them have a real
    source symbol to attach to in the calls table."""
    return [
        FunctionSymbol(name=pa.name, line=pa.line, is_static=False, scope=pa.scope)
        for pa in iter_property_accessor_defs(tree)
    ]


def extract_signals(tree: Tree) -> list[SignalSymbol]:
    signals: list[SignalSymbol] = []
    for scope, subtree in iter_scoped_subtrees(tree):
        if subtree.data != "signal_stmt":
            continue
        name = first_name_token(subtree)
        if name is None:
            continue
        line = getattr(subtree.meta, "line", 0)
        signals.append(SignalSymbol(name=name, line=line, scope=scope))
    return signals


def _first_name_token_deep(node: Tree) -> str | None:
    """Like first_name_token, but also looks one level into a wrapped child
    rule (e.g. const_stmt -> const_assigned -> NAME)."""
    name = first_name_token(node)
    if name is not None:
        return name
    for child in node.children:
        if isinstance(child, Tree):
            name = first_name_token(child)
            if name is not None:
                return name
    return None


def extract_fields(tree: Tree) -> list[FieldSymbol]:
    fields: list[FieldSymbol] = []
    for scope, subtree in iter_scoped_subtrees(tree):
        if subtree.data == "class_var_stmt":
            kind = "var"
        elif subtree.data == "const_stmt":
            kind = "const"
        else:
            continue
        name = _first_name_token_deep(subtree)
        if name is None:
            continue
        line = getattr(subtree.meta, "line", 0)
        fields.append(FieldSymbol(name=name, line=line, kind=kind, scope=scope))
    return fields


def extract_enums(tree: Tree) -> list[EnumSymbol]:
    enums: list[EnumSymbol] = []
    for scope, subtree in iter_scoped_subtrees(tree):
        if subtree.data != "enum_stmt":
            continue
        named = next((c for c in subtree.children if isinstance(c, Tree) and c.data == "enum_named"), None)
        if named is None:
            continue  # anonymous `enum { ... }` -- no usable identifier
        name = first_name_token(named)
        if name is None:
            continue
        line = getattr(subtree.meta, "line", 0)
        enums.append(EnumSymbol(name=name, line=line, scope=scope))
    return enums


def extract_symbols(parse_result: ParseResult) -> FileSymbols:
    if parse_result.tree is None:
        return FileSymbols(
            res_path=parse_result.res_path,
            class_name=None,
            extends=None,
            functions=[],
            signals=[],
        )

    tree = parse_result.tree
    return FileSymbols(
        res_path=parse_result.res_path,
        class_name=extract_class_name(tree),
        extends=extract_extends(tree),
        functions=extract_functions(tree) + extract_property_accessors(tree),
        signals=extract_signals(tree),
        fields=extract_fields(tree),
        enums=extract_enums(tree),
    )

from __future__ import annotations

from dataclasses import dataclass, field

from gdscript_graph.calls import RawCall, RawConnection
from gdscript_graph.engine_api import base_type_name, engine_api
from gdscript_graph.receivers import Receiver
from gdscript_graph.scenes import Scene, SceneIndex
from gdscript_graph.symbols import CallType, FileSymbols

# A resolved static type: (res:// script path, inner class scope), where
# scope None means the script's own top-level class.
TypeRef = tuple[str, "str | None"]

# How many inferred types (a call's return type, typing a receiver that is
# itself a call result...) one lookup may chain through -- also what stops
# a self-referential one, e.g. `var x := x.f()`.
_MAX_INFERENCE_DEPTH = 8


@dataclass
class ResolvedCall:
    source_res_path: str
    source_scope: str | None
    source_function: str
    target_res_path: str
    target_scope: str | None
    target_function: str
    line: int


@dataclass
class UnresolvedCall:
    source_res_path: str
    source_scope: str | None
    source_function: str
    receiver: str | None
    called_name: str
    line: int
    # Calls into the engine, which are expected not to resolve:
    #   "builtin_function"        a global function or constructor: `int()`, `print()`, `Vector2()`,
    #                             or `T.new()`/`super()` where no `_init` is declared
    #   "engine_method"           on self/super/a project class, a method its engine base class has
    #   "engine_receiver"         on an engine/built-in-typed value, class or singleton that has it
    #   "not_a_project_function"  on an unknown receiver, but no project function has this name
    # ...and possibly-missed project calls:
    #   "unknown_receiver"            the receiver's type is unknown (e.g. an untyped variable)
    #   "method_not_found_in_target"  the receiver's project class has no such method, nor its engine base
    reason: str


@dataclass
class ResolvedConnection:
    source_res_path: str
    source_scope: str | None
    source_function: str
    # Where the signal is declared, when it's a project-declared signal;
    # None for an engine built-in (e.g. `$Button.pressed`, a bare
    # `tree_exited`) or a receiver whose type isn't known -- the handler
    # registration is real (and recorded) either way.
    signal_res_path: str | None
    signal_scope: str | None  # the signal's own scope -- equal to source_scope for a bare/
                               # self/inherited reference, else the receiver type's scope
    signal_name: str
    handler_res_path: str
    handler_scope: str | None
    handler_function: str
    line: int


@dataclass
class UnresolvedConnection:
    source_res_path: str
    source_function: str
    signal_receiver: str | None  # None = bare/self/inherited reference
    signal_name: str
    handler_receiver: str | None
    handler_name: str | None  # None = handler argument shape couldn't be parsed
    line: int
    reason: str  # "unknown_receiver" | "method_not_found_in_target" | "unsupported_handler_shape"


@dataclass
class ResolvedSceneConnection:
    scene_res_path: str
    line: int
    signal_name: str
    from_node: str
    to_node: str
    # The signal's declaration on the emitting node's script, when it's a
    # project-declared signal; None for an engine built-in signal (e.g.
    # `Button.pressed`) or one that can't be found.
    signal_res_path: str | None
    handler_res_path: str
    handler_function: str


@dataclass
class UnresolvedSceneConnection:
    scene_res_path: str
    line: int
    signal_name: str
    from_node: str
    to_node: str
    method: str
    reason: str  # "unknown_target_script" | "method_not_found_in_target"


def build_class_name_table(all_symbols: list[FileSymbols]) -> dict[str, str]:
    """Map declared class_name -> res:// path. Later files win on collision."""
    table: dict[str, str] = {}
    for fs in all_symbols:
        if fs.class_name:
            table[fs.class_name] = fs.res_path
    return table


def build_function_index(all_symbols: list[FileSymbols]) -> dict[tuple[str, str | None], set[str]]:
    """Map (res_path, scope) -> function names declared directly in that
    scope. `scope=None` means top-level (file-level) functions."""
    index: dict[tuple[str, str | None], set[str]] = {}
    for fs in all_symbols:
        for func in fs.functions:
            index.setdefault((fs.res_path, func.scope), set()).add(func.name)
    return index


def build_signal_index(all_symbols: list[FileSymbols]) -> dict[tuple[str, str | None], set[str]]:
    """Map (res_path, scope) -> signal names declared directly in that
    scope. `scope=None` means top-level (file-level) signals. Mirrors
    `build_function_index`, used to resolve a signal accessed through a
    chained receiver (`<receiver>.signal_name.connect(...)`) the same way
    a chained method call is resolved."""
    index: dict[tuple[str, str | None], set[str]] = {}
    for fs in all_symbols:
        for sig in fs.signals:
            index.setdefault((fs.res_path, sig.scope), set()).add(sig.name)
    return index


def build_inheritance_map(
    all_symbols: list[FileSymbols], class_name_table: dict[str, str]
) -> dict[str, str | None]:
    """Map res_path -> parent res_path, resolved from each file's top-level
    ``extends`` when it names another script (a res:// path or a declared
    class_name); None otherwise -- an engine built-in, or a class nested in
    another script (see `_parent_type`, which also resolves that)."""
    known_paths = {fs.res_path for fs in all_symbols}
    parents: dict[str, str | None] = {}
    for fs in all_symbols:
        extends = fs.extends
        if extends is None:
            parents[fs.res_path] = None
        elif extends.startswith("res://"):
            parents[fs.res_path] = extends if extends in known_paths else None
        else:
            parents[fs.res_path] = class_name_table.get(extends)
    return parents


@dataclass
class ProjectIndex:
    """Every project-wide table resolution needs, built once per build."""
    class_name_table: dict[str, str]
    autoloads: dict[str, str]
    function_index: dict[tuple[str, str | None], set[str]]
    signal_index: dict[tuple[str, str | None], set[str]]
    inheritance_map: dict[str, str | None]
    # (res_path, scope) -> member var/const name -> its raw static type (a
    # type name as written in that file, a res:// path, a `CallType`, or
    # None) -- see `FieldSymbol.type_name`.
    member_types: dict[tuple[str, str | None], dict[str, str | CallType | None]]
    # (res_path, scope, function name) -> its `-> T` annotation as written.
    return_types: dict[tuple[str, str | None, str], str | None]
    # res_path -> its top-level `extends` as written (None = none).
    extends_by_path: dict[str, str | None]
    # Every function name any project file declares.
    function_names: set[str]
    # (res_path, scope) -> names of the consts among those members, the
    # only members that can stand in for a type (`const E = preload(...)`).
    const_names: dict[tuple[str, str | None], set[str]]
    # Every (res_path, inner class scope).
    inner_classes: set[tuple[str, str]]
    # (res_path, inner class scope) -> its own `extends` as written.
    class_extends: dict[tuple[str, str], str | None]
    known_paths: set[str]
    # Memo for `_type_ancestors` -- walked for nearly every call resolved.
    ancestors_cache: dict[TypeRef, list[TypeRef]] = field(default_factory=dict, repr=False)


def build_project_index(all_symbols: list[FileSymbols], autoloads: dict[str, str]) -> ProjectIndex:
    class_name_table = build_class_name_table(all_symbols)
    function_index = build_function_index(all_symbols)
    member_types: dict[tuple[str, str | None], dict[str, str | CallType | None]] = {}
    return_types: dict[tuple[str, str | None, str], str | None] = {}
    const_names: dict[tuple[str, str | None], set[str]] = {}
    inner_classes: set[tuple[str, str]] = set()
    class_extends: dict[tuple[str, str], str | None] = {}
    for fs in all_symbols:
        for inner_scope, extends in fs.class_extends.items():
            class_extends[(fs.res_path, inner_scope)] = extends
            inner_classes.add((fs.res_path, inner_scope))
        for func in fs.functions:
            return_types.setdefault((fs.res_path, func.scope, func.name), func.return_type)
        for fld in fs.fields:
            # First declaration wins, same as symbol ids in db.py.
            member_types.setdefault((fs.res_path, fld.scope), {}).setdefault(fld.name, fld.type_name)
            if fld.kind == "const":
                const_names.setdefault((fs.res_path, fld.scope), set()).add(fld.name)
        for decl in (*fs.functions, *fs.signals, *fs.fields, *fs.enums):
            scope = decl.scope
            while scope:
                inner_classes.add((fs.res_path, scope))
                scope = scope.rpartition(".")[0] or None
    return ProjectIndex(
        class_name_table=class_name_table,
        autoloads=autoloads,
        function_index=function_index,
        signal_index=build_signal_index(all_symbols),
        inheritance_map=build_inheritance_map(all_symbols, class_name_table),
        member_types=member_types,
        return_types=return_types,
        extends_by_path={fs.res_path: fs.extends for fs in all_symbols},
        function_names={name for names in function_index.values() for name in names},
        const_names=const_names,
        inner_classes=inner_classes,
        class_extends=class_extends,
        known_paths={fs.res_path for fs in all_symbols},
    )


def _enclosing_scopes(scope: str | None) -> list[str | None]:
    """`scope` and every scope enclosing it, innermost first, ending with
    None (top level): "A.B" -> ["A.B", "A", None]."""
    scopes: list[str | None] = []
    while scope:
        scopes.append(scope)
        scope = scope.rpartition(".")[0] or None
    scopes.append(None)
    return scopes


def _ancestor_paths(index: ProjectIndex, path: str | None) -> list[str]:
    """`path` and the scripts its top-level `extends` chain runs through,
    nearest first -- scripts only (see `_type_ancestors` for classes)."""
    paths: list[str] = []
    while path is not None and path not in paths:
        paths.append(path)
        path = index.inheritance_map.get(path)
    return paths


def _parent_type(index: ProjectIndex, owner: TypeRef) -> TypeRef | None:
    """The project class `owner` extends: for a script, the one its
    top-level `extends` names -- including a class nested in another
    script (`extends Base.State`); for an inner class, its own `extends`,
    resolved where the class is declared. None for an engine base (or none,
    or one that can't be found)."""
    path, scope = owner
    if scope is None:
        parent = index.inheritance_map.get(path)
        if parent is not None:
            return (parent, None)
        extends = index.extends_by_path.get(path)
        if extends is not None and "." in extends and not extends.startswith("res://"):
            return resolve_type_name(index, extends, path, None)
        return None
    extends = index.class_extends.get((path, scope))
    if extends is None:
        return None
    if extends.startswith("res://"):
        return (extends, None) if extends in index.known_paths else None
    return resolve_type_name(index, extends, path, scope.rpartition(".")[0] or None)


def _type_ancestors(index: ProjectIndex, owner: TypeRef) -> list[TypeRef]:
    """`owner` and every project class it inherits from, nearest first."""
    cached = index.ancestors_cache.get(owner)
    if cached is not None:
        return cached
    chain: list[TypeRef] = []
    current: TypeRef | None = owner
    while current is not None and current not in chain:
        chain.append(current)
        current = _parent_type(index, current)
    index.ancestors_cache[owner] = chain
    return chain


def resolve_type_name(index: ProjectIndex, name: str, res_path: str, scope: str | None) -> TypeRef | None:
    """Resolve a type as written in `res_path` at `scope` -- in a type
    annotation, an `as` cast, or a `T.new()` -- to the class it names, the
    way GDScript does: a res:// script path (from `preload`) as-is; then a
    const preload alias or inner class visible from `scope` (its own and
    enclosing classes, then consts inherited from the top-level `extends`
    chain); then a global `class_name`. `Outer.Inner` resolves `Outer`
    first. Engine/built-in types (and anything unknown) -> None."""
    if name.startswith("res://"):
        return (name, None) if name in index.known_paths else None

    if "." in name:
        outer_name, _, inner_name = name.partition(".")
        outer = resolve_type_name(index, outer_name, res_path, scope)
        if outer is None:
            return None
        inner_scope = f"{outer[1]}.{inner_name}" if outer[1] else inner_name
        return (outer[0], inner_scope) if (outer[0], inner_scope) in index.inner_classes else None

    for s in _enclosing_scopes(scope):
        if name in index.const_names.get((res_path, s), ()):
            return _const_alias(index, res_path, s, name)
        inner_scope = f"{s}.{name}" if s else name
        if (res_path, inner_scope) in index.inner_classes:
            return (res_path, inner_scope)
    for ancestor in _ancestor_paths(index, index.inheritance_map.get(res_path)):
        if name in index.const_names.get((ancestor, None), ()):
            return _const_alias(index, ancestor, None, name)

    path = index.class_name_table.get(name)
    return (path, None) if path is not None else None


def _const_alias(index: ProjectIndex, res_path: str, scope: str | None, name: str) -> TypeRef | None:
    """The script a `const <name> = preload("x.gd")` stands for; None for
    any other const (e.g. `const MAX = 5`), which isn't a type at all."""
    value_type = index.member_types[(res_path, scope)][name]
    if not isinstance(value_type, str) or not value_type.startswith("res://"):
        return None
    return (value_type, None) if value_type in index.known_paths else None


def lookup_member(
    index: ProjectIndex, owner: TypeRef, name: str, depth: int = 0
) -> tuple[bool, TypeRef | None]:
    """Find member var/const `name` on class `owner`, walking its `extends`
    chain. Returns (found, the member's resolved static type), the type
    resolved in the *declaring* class's own scope."""
    found, static_type, site = _find_member(index, owner, name)
    if not found:
        return False, None
    return True, resolve_static_type(index, site, static_type, depth)


def _find_member(index: ProjectIndex, owner: TypeRef, name: str) -> tuple[bool, str | CallType | None, _CallSite]:
    """(found, the member's static type as extracted, a site in its
    declaring class -- where that type is to be resolved)."""
    for owner_path, owner_scope in _type_ancestors(index, owner):
        members = index.member_types.get((owner_path, owner_scope), {})
        if name in members:
            return True, members[name], _CallSite(owner_path, owner_scope, {}, frozenset(), False)
    return False, None, _CallSite(owner[0], owner[1], {}, frozenset(), False)


def find_method(index: ProjectIndex, owner: TypeRef, name: str) -> TypeRef | None:
    """Where function `name` is declared, called on an instance of
    `owner` -- it or the nearest class it inherits from that declares it:
    (declaring path, declaring scope), or None."""
    for ancestor in _type_ancestors(index, owner):
        if name in index.function_index.get(ancestor, ()):
            return ancestor
    return None


def find_signal(index: ProjectIndex, owner: TypeRef, name: str) -> TypeRef | None:
    """Like `find_method`, for a signal."""
    for ancestor in _type_ancestors(index, owner):
        if name in index.signal_index.get(ancestor, ()):
            return ancestor
    return None


@dataclass
class _CallSite:
    """Where a receiver expression appears -- what its names can refer to."""
    res_path: str
    scope: str | None
    local_types: dict[str, str | CallType | None]
    lambda_shadowed: set[str] | frozenset[str]
    in_lambda: bool


def resolve_static_type(
    index: ProjectIndex, site: _CallSite, static_type: str | CallType | None, depth: int = 0
) -> TypeRef | None:
    """Resolve an extracted static type (see `FieldSymbol.type_name`) as it
    stands at `site`: a type name as written there, or a call's return
    type."""
    if static_type is None:
        return None
    if isinstance(static_type, CallType):
        return _call_return_type(index, site, static_type, depth + 1)
    return resolve_type_name(index, static_type, site.res_path, site.scope)


def _call_target(
    index: ProjectIndex, site: _CallSite, call: CallType, depth: int
) -> tuple[TypeRef | None, TypeRef | None]:
    """(where the project function `call` reaches from `site` is declared
    -- found exactly as resolve_calls finds a call's target -- or None; the
    project class it's called on, when that's known)."""
    if call.receiver is None:
        own = (site.res_path, site.scope)
        return find_method(index, own, call.name), own
    owner, _ = resolve_receiver(index, site, call.receiver, depth)
    if owner is None:
        return None, None
    return find_method(index, owner, call.name), owner


def _call_return_type(index: ProjectIndex, site: _CallSite, call: CallType, depth: int) -> TypeRef | None:
    """The declared (`-> T`) return type of the function `call` reaches
    from `site`, resolved in the declaring file's scope. None when the
    function, or a return type naming a project class, can't be found."""
    if depth > _MAX_INFERENCE_DEPTH:
        return None
    declaring, owner = _call_target(index, site, call, depth)
    if call.name == "new" and call.receiver is not None and declaring is None:
        return owner  # `T.new()`: an instance of T
    if declaring is None:
        return None
    return_type = index.return_types.get((declaring[0], declaring[1], call.name))
    if return_type is None:
        return None
    return resolve_type_name(index, return_type, declaring[0], declaring[1])


def _engine_base(index: ProjectIndex, owner: TypeRef) -> str:
    """The engine class a project class ultimately extends: the `extends`
    of the top of its project `extends` chain -- RefCounted when there's
    none, as in Godot. An unknown base could be anything, so it just gets
    Object's methods."""
    top_path, top_scope = _type_ancestors(index, owner)[-1]
    extends = index.extends_by_path.get(top_path) if top_scope is None else index.class_extends.get((top_path, top_scope))
    if extends is None:
        return "RefCounted"
    base = base_type_name(extends)
    return base if engine_api().is_type(base) else "Object"


def _engine_type(type_name: str | None) -> str | None:
    if not type_name:
        return None
    base = base_type_name(type_name)
    return base if engine_api().is_type(base) else None


def _engine_receiver_type(index: ProjectIndex, site: _CallSite, receiver: Receiver, depth: int = 0) -> str | None:
    """The engine/built-in type a receiver has, when it's statically known
    to be one: a variable/parameter/member declared as one, the result of a
    call that returns one, or an engine class or singleton named directly
    (`Time.get_ticks_msec()`, `Input.is_action_pressed()`)."""
    if receiver.cast is not None:
        return _engine_type(receiver.cast) if not receiver.chain else None
    if receiver.call is not None:
        return _engine_type(_raw_type(index, site, receiver.call, depth + 1)) if not receiver.chain else None
    chain = receiver.chain
    if len(chain) == 2 and chain[0] == "self":
        found, static_type, member_site = _find_member(index, (site.res_path, site.scope), chain[1])
        return _engine_type(_raw_type(index, member_site, static_type, depth)) if found else None
    if len(chain) != 1:
        return None
    name = chain[0]
    if not (site.in_lambda and name in site.lambda_shadowed):
        if name in site.local_types:
            return _engine_type(_raw_type(index, site, site.local_types[name], depth))
        found, static_type, member_site = _find_member(index, (site.res_path, site.scope), name)
        if found:
            return _engine_type(_raw_type(index, member_site, static_type, depth))
    if name in index.autoloads or resolve_type_name(index, name, site.res_path, site.scope) is not None:
        return None
    api = engine_api()
    return name if api.is_type(name) or name in api.singletons else None


def _raw_type(index: ProjectIndex, site: _CallSite, static_type: str | CallType | None, depth: int) -> str | None:
    """A static type as a type name -- for a call, the declared return type
    of the project function, or the engine function, it reaches."""
    if static_type is None or depth > _MAX_INFERENCE_DEPTH:
        return None
    if isinstance(static_type, str):
        return static_type
    call = static_type
    declaring, owner = _call_target(index, site, call, depth + 1)
    if declaring is not None:
        return index.return_types.get((declaring[0], declaring[1], call.name))
    api = engine_api()
    if call.name == "new" and call.receiver is not None and owner is None:
        return _engine_receiver_type(index, site, call.receiver, depth + 1)  # `Node.new()`: a Node
    if call.receiver is None and call.name in api.global_functions:
        return api.global_functions[call.name] or None
    if call.receiver is None and api.is_type(call.name):
        return call.name  # a constructor: `Vector2(...)`
    engine_owner = (
        _engine_base(index, owner) if owner is not None
        else _engine_receiver_type(index, site, call.receiver, depth + 1) if call.receiver is not None
        else None
    )
    return (api.method_return_type(engine_owner, call.name) or None) if engine_owner is not None else None


def _unresolved_call_reason(
    index: ProjectIndex, site: _CallSite, call: RawCall, receiver_type: TypeRef | None
) -> str:
    """Why `call` resolved to no project function -- see `UnresolvedCall.reason`."""
    api = engine_api()
    name = call.called_name
    if (call.receiver is None and name == "super") or (call.receiver is not None and name == "new"):
        return "builtin_function"  # a constructor with no project `_init` to run
    if call.receiver in (None, "self", "super"):
        if call.receiver is None and (name in api.global_functions or api.is_type(name)):
            return "builtin_function"
        if api.has_method(_engine_base(index, (site.res_path, site.scope)), name):
            return "engine_method"
        return "method_not_found_in_target"
    if receiver_type is not None:
        if api.has_method(_engine_base(index, receiver_type), name):
            return "engine_method"
        return "method_not_found_in_target"
    if call.receiver_expr is not None:
        engine_type = _engine_receiver_type(index, site, call.receiver_expr)
        if engine_type is not None and api.has_method(engine_type, name):
            return "engine_receiver"
    if name not in index.function_names:
        return "not_a_project_function"
    return "unknown_receiver"


def _resolve_base_name(
    index: ProjectIndex, site: _CallSite, name: str, depth: int = 0
) -> tuple[TypeRef | None, bool]:
    """Type of a receiver chain's first name, in GDScript's own lookup
    order: `self`; a local var/param; a member var/const of the enclosing
    class (inherited ones included); an autoload; a type name (const
    preload alias, inner class, `class_name`). A local or member shadows
    any same-named global -- if its type can't be resolved, the receiver
    is unknown rather than falling through to the global.

    Inside a lambda, a name the lambda re-declares can't be typed from the
    enclosing function's locals/members (the lambda's own declaration may
    differ, and isn't tracked), so it skips straight to the globals.

    Returns (type, recognized): recognized is False when the name is
    unknown or its type isn't a project class (e.g. an engine type)."""
    if name == "self":
        return (site.res_path, site.scope), True
    if not (site.in_lambda and name in site.lambda_shadowed):
        if name in site.local_types:
            resolved = resolve_static_type(index, site, site.local_types[name], depth)
            return resolved, resolved is not None
        found, member_type = lookup_member(index, (site.res_path, site.scope), name, depth)
        if found:
            return member_type, member_type is not None
    if name in index.autoloads:
        return (index.autoloads[name], None), True
    resolved = resolve_type_name(index, name, site.res_path, site.scope)
    return resolved, resolved is not None


def resolve_receiver(
    index: ProjectIndex, site: _CallSite, receiver: Receiver, depth: int = 0
) -> tuple[TypeRef | None, bool]:
    """Static type of a receiver expression: its cast or first name (see
    `_resolve_base_name`), then each following field through the previous
    one's declared type. Returns (type, recognized) as `_resolve_base_name`
    does -- an untyped or undeclared field anywhere in the chain makes the
    whole receiver unknown."""
    if receiver.cast is not None:
        current = resolve_type_name(index, receiver.cast, site.res_path, site.scope)
        fields = receiver.chain
    elif receiver.call is not None:
        current = _call_return_type(index, site, receiver.call, depth + 1)
        fields = receiver.chain
    else:
        current, recognized = _resolve_base_name(index, site, receiver.chain[0], depth)
        if current is None:
            return None, recognized
        fields = receiver.chain[1:]
    for field_name in fields:
        if current is None:
            break
        _, current = lookup_member(index, current, field_name, depth)
    return current, current is not None


def resolve_calls(
    file_symbols: FileSymbols,
    raw_calls: list[RawCall],
    index: ProjectIndex,
    local_var_types: dict[tuple[str | None, str], dict[str, str | CallType | None]],
    lambda_shadowed_names: dict[tuple[str | None, str], set[str]] | None = None,
) -> tuple[list[ResolvedCall], list[UnresolvedCall]]:
    """Resolve raw call sites in one file to (target file, target function).

    Resolution covers, in GDScript's actual scoping order: same-scope calls
    (bare or ``self.``, falling back up the top-level ``extends`` chain when
    the caller itself is top-level), ``super.`` calls (parent chain only,
    top-level callers only), and calls on a typed receiver (see
    `resolve_receiver`: a local var/param, member var/const, autoload, or
    type name -- in that order, so a local or member always shadows a
    same-named global -- possibly followed by a chain of typed fields, or
    an ``(<expr> as T)`` cast) -- walking the target's inheritance chain.
    Calls through an untyped variable/field, an engine-typed receiver, or a
    built-in Godot API are left unresolved. A call made inside a lambda,
    through a name that some lambda in the same function re-declares,
    doesn't trust the enclosing function's (possibly stale) type for that
    name -- it falls through to autoload/class_name resolution instead,
    same as if it weren't a known local/member at all.
    """
    lambda_shadowed_names = lambda_shadowed_names or {}
    resolved: list[ResolvedCall] = []
    unresolved: list[UnresolvedCall] = []
    source_path = file_symbols.res_path

    for call in raw_calls:
        site = _CallSite(
            res_path=source_path,
            scope=call.caller_scope,
            local_types=local_var_types.get((call.caller_scope, call.caller_function), {}),
            lambda_shadowed=lambda_shadowed_names.get((call.caller_scope, call.caller_function), frozenset()),
            in_lambda=call.in_lambda,
        )
        own = (source_path, call.caller_scope)
        target: TypeRef | None = None
        receiver_type: TypeRef | None = None

        target_function = call.called_name
        if call.receiver is None and call.called_name == "super":
            # `super(...)` in a constructor runs the parent's `_init`.
            target_function = "_init"
            parent = _parent_type(index, own)
            target = find_method(index, parent, "_init") if parent is not None else None
        elif call.receiver is None or call.receiver == "self":
            target = find_method(index, own, call.called_name)
        elif call.receiver == "super":
            parent = _parent_type(index, own)
            target = find_method(index, parent, call.called_name) if parent is not None else None
        elif call.receiver_expr is not None:
            receiver_type, _ = resolve_receiver(index, site, call.receiver_expr)
            if receiver_type is not None:
                # `T.new(...)` runs T's `_init` (an inherited one included):
                # a call site that breaks when `_init`'s signature changes.
                method = "_init" if call.called_name == "new" else call.called_name
                target = find_method(index, receiver_type, method)
                if target is not None:
                    target_function = method

        if target is not None:
            resolved.append(ResolvedCall(
                source_res_path=source_path,
                source_scope=call.caller_scope,
                source_function=call.caller_function,
                target_res_path=target[0],
                target_scope=target[1],
                target_function=target_function,
                line=call.line,
            ))
            continue

        unresolved.append(UnresolvedCall(
            source_res_path=source_path,
            source_scope=call.caller_scope,
            source_function=call.caller_function,
            receiver=call.receiver,
            called_name=call.called_name,
            line=call.line,
            reason=_unresolved_call_reason(index, site, call, receiver_type),
        ))

    return resolved, unresolved


def resolve_signal_connections(
    file_symbols: FileSymbols,
    raw_connections: list[RawConnection],
    index: ProjectIndex,
    local_var_types: dict[tuple[str | None, str], dict[str, str | CallType | None]] | None = None,
    lambda_shadowed_names: dict[tuple[str | None, str], set[str]] | None = None,
) -> tuple[list[ResolvedConnection], list[UnresolvedConnection]]:
    """Resolve ``<signal>.connect(<handler>)`` sites to the function that
    handles the signal. The handler reference is resolved exact-scope-first
    for a bare/``self.`` handler, otherwise through `resolve_receiver`
    exactly like a call receiver (a local or member shadows a same-named
    global, and a ``.connect()`` made inside a lambda doesn't trust the
    enclosing function's (possibly stale) type for a name some lambda
    re-declares).

    A connection counts as resolved whenever its handler does. The signal
    side is linked to its declaration when it's a project-declared signal:
    either in the same scope as the ``.connect()`` call (a bare/self
    reference; at top level, also one inherited up the ``extends``
    chain), or accessed through a receiver
    (`conn.signal_receiver` set, e.g. `GameManager.card_drawn.connect(...)`,
    `self.hud.closed.connect(...)`) whose type resolves the same way a call
    receiver's does, then searched for the signal (up its top-level
    inheritance chain). Anything else -- an engine built-in signal, or a
    receiver of unknown type -- leaves the signal unlinked, not the whole
    connection: the handler is registered all the same, and must not look
    like dead code."""
    local_var_types = local_var_types or {}
    lambda_shadowed_names = lambda_shadowed_names or {}
    resolved: list[ResolvedConnection] = []
    unresolved: list[UnresolvedConnection] = []
    source_path = file_symbols.res_path

    for conn in raw_connections:
        if conn.handler_name is None:
            unresolved.append(UnresolvedConnection(
                source_res_path=source_path,
                source_function=conn.caller_function,
                signal_receiver=conn.signal_receiver,
                signal_name=conn.signal_name,
                handler_receiver=conn.handler_receiver,
                handler_name=None,
                line=conn.line,
                reason="unsupported_handler_shape",
            ))
            continue

        site = _CallSite(
            res_path=source_path,
            scope=conn.caller_scope,
            local_types=local_var_types.get((conn.caller_scope, conn.caller_function), {}),
            lambda_shadowed=lambda_shadowed_names.get((conn.caller_scope, conn.caller_function), frozenset()),
            in_lambda=conn.in_lambda,
        )

        signal_ref: TypeRef | None = None
        if conn.signal_receiver is None:
            signal_ref = find_signal(index, (source_path, conn.caller_scope), conn.signal_name)
        elif conn.signal_receiver_expr is not None:
            signal_owner, _ = resolve_receiver(index, site, conn.signal_receiver_expr)
            if signal_owner is not None:
                signal_ref = find_signal(index, signal_owner, conn.signal_name)
        signal_res_path, signal_scope = signal_ref if signal_ref is not None else (None, None)

        target_path: str | None = None
        target_scope: str | None = None
        receiver_recognized = True

        if conn.handler_receiver is None or conn.handler_receiver == "self":
            target = find_method(index, (source_path, conn.caller_scope), conn.handler_name)
            if target is not None:
                target_path, target_scope = target
        elif conn.handler_receiver_expr is not None:
            handler_owner, receiver_recognized = resolve_receiver(index, site, conn.handler_receiver_expr)
            if handler_owner is not None:
                target = find_method(index, handler_owner, conn.handler_name)
                if target is not None:
                    target_path, target_scope = target
        else:
            receiver_recognized = False

        if target_path is not None:
            resolved.append(ResolvedConnection(
                source_res_path=source_path,
                source_scope=conn.caller_scope,
                source_function=conn.caller_function,
                signal_res_path=signal_res_path,
                signal_scope=signal_scope,
                signal_name=conn.signal_name,
                handler_res_path=target_path,
                handler_scope=target_scope,
                handler_function=conn.handler_name,
                line=conn.line,
            ))
            continue

        reason = "method_not_found_in_target" if receiver_recognized else "unknown_receiver"
        unresolved.append(UnresolvedConnection(
            source_res_path=source_path,
            source_function=conn.caller_function,
            signal_receiver=conn.signal_receiver,
            signal_name=conn.signal_name,
            handler_receiver=conn.handler_receiver,
            handler_name=conn.handler_name,
            line=conn.line,
            reason=reason,
        ))

    return resolved, unresolved


def resolve_scene_connections(
    scene: Scene, scenes: SceneIndex, index: ProjectIndex
) -> tuple[list[ResolvedSceneConnection], list[UnresolvedSceneConnection]]:
    """Resolve a scene's editor-made `[connection signal=... from=... to=...
    method=...]` entries to the handler function on the `to` node's script
    (up its inheritance chain), and -- when it's one -- the project-declared
    signal on the `from` node's script. Godot calls these handlers itself,
    so without them a handler connected only in the editor would look like
    it has no callers at all."""
    resolved: list[ResolvedSceneConnection] = []
    unresolved: list[UnresolvedSceneConnection] = []
    for conn in scene.connections:
        target_script = scenes.script_for_node(scene.res_path, conn.to_node)
        handler = None
        reason = "unknown_target_script"
        if target_script is not None and target_script in index.known_paths:
            handler = find_method(index, (target_script, None), conn.method)
            reason = "method_not_found_in_target"
        if handler is None:
            unresolved.append(UnresolvedSceneConnection(
                scene_res_path=scene.res_path,
                line=conn.line,
                signal_name=conn.signal,
                from_node=conn.from_node,
                to_node=conn.to_node,
                method=conn.method,
                reason=reason,
            ))
            continue

        source_script = scenes.script_for_node(scene.res_path, conn.from_node)
        signal_ref = None
        if source_script is not None and source_script in index.known_paths:
            signal_ref = find_signal(index, (source_script, None), conn.signal)
        resolved.append(ResolvedSceneConnection(
            scene_res_path=scene.res_path,
            line=conn.line,
            signal_name=conn.signal,
            from_node=conn.from_node,
            to_node=conn.to_node,
            signal_res_path=signal_ref[0] if signal_ref is not None else None,
            handler_res_path=handler[0],
            handler_function=conn.method,
        ))
    return resolved, unresolved

from __future__ import annotations

from dataclasses import dataclass

from lark import Token, Tree

from gdscript_graph.symbols import iter_function_defs, iter_property_accessor_defs

_NAME_TOKEN_TYPES = ("NAME", "GET", "SET")
_CONNECT_METHOD = "connect"

# Sentinel receiver for a getattr base we can't attribute to a single name:
# either the base is itself an expression (e.g. get_node("X").foo()) or the
# attribute path has 2+ segments after the base (e.g. self.child.foo()).
# Resolving either correctly would require tracking field types, which is
# out of scope for v1 -- this string can never collide with a real GDScript
# identifier, so it always falls through resolution as "unknown_receiver"
# instead of being silently misattributed to the wrong segment.
_CHAINED_RECEIVER = "<chained>"


@dataclass
class RawCall:
    caller_scope: str | None
    caller_function: str
    caller_line: int
    receiver: str | None  # None = standalone call; see _CHAINED_RECEIVER for chains
    called_name: str
    line: int
    in_lambda: bool = False  # True if this call site is nested inside a lambda


@dataclass
class RawConnection:
    caller_scope: str | None
    caller_function: str
    line: int
    signal_res_path: str | None  # the file the signal is actually declared in -- known at
                                  # extraction time for a bare/self/inherited reference; None
                                  # when signal_receiver is set, meaning it must be resolved
                                  # later from the receiver's type (autoload/class_name/local)
    signal_name: str
    signal_receiver: str | None  # None = bare/self/inherited reference (signal_res_path
                                  # already known); otherwise the base identifier of a
                                  # `<signal_receiver>.<signal_name>.connect(...)` chain
    handler_receiver: str | None  # None = handler in current scope (bare name or self.)
    handler_name: str | None  # None = handler argument shape isn't a simple name/getattr (e.g. a lambda)
    in_lambda: bool = False  # True if the .connect() call site is nested inside a lambda


def _standalone_call_name(node: Tree) -> str | None:
    if not node.children:
        return None
    first = node.children[0]
    return str(first) if isinstance(first, Token) else None


def _parse_getattr(getattr_tree: Tree) -> tuple[str | None, str | None]:
    children = getattr_tree.children
    if not children:
        return None, None
    base = children[0]
    name_tokens = [
        c for c in children[1:] if isinstance(c, Token) and c.type in _NAME_TOKEN_TYPES
    ]
    method = str(name_tokens[-1]) if name_tokens else None
    if not isinstance(base, Token) or len(name_tokens) > 1:
        return _CHAINED_RECEIVER, method
    return str(base), method


def _parse_chained_signal_connect(getattr_tree: Tree) -> tuple[str, str] | None:
    """For a getattr chain shaped exactly `<receiver>.<signal_name>.connect`
    (e.g. `GameManager.card_drawn.connect(...)`), return
    (receiver, signal_name). Returns None for any other shape -- a bare/
    self signal reference has no receiver segment to extract here (handled
    separately), and any chain that isn't exactly this 2-name-segment shape
    isn't a signal-connect pattern this can recognize.

    This is checked independently of `_parse_getattr` (which collapses any
    2+-segment chain to `_CHAINED_RECEIVER` for ordinary call resolution,
    since resolving an arbitrary chained call target would need field-type
    tracking that's out of scope for v1) -- connecting to a signal accessed
    through an autoload, `class_name`, or locally-typed variable (e.g.
    `GameManager.card_drawn.connect(...)`, `unit.died.connect(...)`) is by
    far the most common real-world signal-wiring idiom, so it's worth
    resolving specifically even though the general chained-call case isn't.
    Whether `<receiver>` is actually a recognized autoload/class_name/local
    and whether `<signal_name>` is genuinely a declared signal on it are
    both verified later during resolution, not here -- this only recognizes
    the syntactic shape."""
    children = getattr_tree.children
    if not children:
        return None
    base = children[0]
    name_tokens = [
        c for c in children[1:] if isinstance(c, Token) and c.type in _NAME_TOKEN_TYPES
    ]
    if not isinstance(base, Token) or len(name_tokens) != 2:
        return None
    if str(name_tokens[1]) != _CONNECT_METHOD:
        return None
    return str(base), str(name_tokens[0])


def _parse_callable_arg(arg: object) -> tuple[str | None, str | None]:
    """Parse a bare handler reference passed to .connect(), e.g. `_on_died`
    or `self._on_died`. Returns (receiver, name), or (None, None) if the
    argument isn't a simple name/getattr reference (e.g. a lambda)."""
    if isinstance(arg, Token) and arg.type == "NAME":
        return None, str(arg)
    if isinstance(arg, Tree) and arg.data == "getattr":
        return _parse_getattr(arg)
    return None, None


def _walk_body(
    node: object,
    scope: str | None,
    caller_name: str,
    caller_line: int,
    signal_names: dict[str, str],  # signal name -> the res:// path it's actually declared in
    calls: list[RawCall],
    connections: list[RawConnection],
    in_lambda: bool = False,
) -> None:
    if not isinstance(node, Tree):
        return

    if node.data == "standalone_call":
        name = _standalone_call_name(node)
        if name is not None:
            calls.append(RawCall(
                caller_scope=scope,
                caller_function=caller_name,
                caller_line=caller_line,
                receiver=None,
                called_name=name,
                line=getattr(node.meta, "line", 0),
                in_lambda=in_lambda,
            ))
    elif node.data == "getattr_call":
        getattr_node = node.children[0] if node.children else None
        if isinstance(getattr_node, Tree) and getattr_node.data == "getattr":
            has_arg = len(node.children) >= 2
            chained_connect = _parse_chained_signal_connect(getattr_node) if has_arg else None
            if chained_connect is not None and chained_connect[0] == "self":
                # `self.<signal>.connect(...)` means exactly the same thing
                # as bare `<signal>.connect(...)` -- "self" can never
                # legitimately be an autoload/class_name/local-var
                # identifier, so route it through the bare-form lookup
                # below instead of chain resolution (where it could never
                # match anything and would always stay unresolved).
                receiver, method = chained_connect[1], _CONNECT_METHOD
            elif chained_connect is not None:
                signal_receiver, signal_name = chained_connect
                handler_receiver, handler_name = _parse_callable_arg(node.children[1])
                connections.append(RawConnection(
                    caller_scope=scope,
                    caller_function=caller_name,
                    line=getattr(node.meta, "line", 0),
                    signal_res_path=None,
                    signal_name=signal_name,
                    signal_receiver=signal_receiver,
                    handler_receiver=handler_receiver,
                    handler_name=handler_name,
                    in_lambda=in_lambda,
                ))
                # Once recognized as a connect() site (even one whose
                # signal receiver might fail to resolve later), never also
                # record it as a generic call to a same-named method.
                receiver, method = None, None
            else:
                receiver, method = _parse_getattr(getattr_node)

            if method == _CONNECT_METHOD and receiver in signal_names and has_arg:
                handler_receiver, handler_name = _parse_callable_arg(node.children[1])
                connections.append(RawConnection(
                    caller_scope=scope,
                    caller_function=caller_name,
                    line=getattr(node.meta, "line", 0),
                    signal_res_path=signal_names[receiver],
                    signal_name=receiver,
                    signal_receiver=None,
                    handler_receiver=handler_receiver,
                    handler_name=handler_name,
                    in_lambda=in_lambda,
                ))
                # Once recognized as a connect() site on a known signal,
                # never also record it as a generic call -- even when the
                # handler shape couldn't be parsed (e.g. an inline
                # lambda), it isn't a plain "unresolved call to connect()".
                method = None
            if method is not None:
                calls.append(RawCall(
                    caller_scope=scope,
                    caller_function=caller_name,
                    caller_line=caller_line,
                    receiver=receiver,
                    called_name=method,
                    line=getattr(node.meta, "line", 0),
                    in_lambda=in_lambda,
                ))

    # Recurse unconditionally so calls nested in arguments or in a chained
    # getattr base (e.g. get_node("X").foo()) are still found. Once inside
    # a lambda, every descendant call stays flagged in_lambda=True, even
    # across nested lambdas.
    child_in_lambda = in_lambda or node.data == "lambda"
    for child in node.children:
        _walk_body(child, scope, caller_name, caller_line, signal_names, calls, connections, child_in_lambda)


def extract_calls_and_connections(
    tree: Tree, signal_names_by_scope: dict[str | None, dict[str, str]] | None = None
) -> tuple[list[RawCall], list[RawConnection]]:
    """Extract call sites and `<signal>.connect(<handler>)` registrations,
    grouped by their enclosing function.

    Only calls inside a function body are tracked -- calls in class-level
    var initializers (rare) are out of scope for v1. Two connection shapes
    are recognized: the simple `signal_name.connect(handler)` form, only
    when the signal is declared in the SAME scope as the connect() call --
    either the current file's own scope, or (top-level only) inherited
    from an ancestor class, per `signal_names_by_scope`, which maps each
    recognized name to the res:// path it's actually declared in (a bare
    name still can't reach a signal declared in an unrelated outer/inner
    class, mirroring how bare function calls don't cross scopes either) --
    and `<receiver>.signal_name.connect(handler)`, the far more common
    real-world idiom (an autoload, `class_name`, or locally-typed-variable
    receiver, e.g. `GameManager.card_drawn.connect(...)`), recognized here
    by shape and resolved later in `resolve.py` once the receiver's actual
    type is known. Any other chained receiver (e.g.
    `self.child.signal_name.connect(...)`) falls through to the generic
    getattr_call handling, same as other unresolvable chains.
    """
    signal_names_by_scope = signal_names_by_scope or {}
    calls: list[RawCall] = []
    connections: list[RawConnection] = []

    for fd in iter_function_defs(tree):
        scope_signals = signal_names_by_scope.get(fd.scope, {})
        # Walk children[0] (func_header) too, not just the body -- a
        # default-argument-value expression (`func f(x = some_call()):`)
        # can itself contain a call, which would otherwise vanish from the
        # graph entirely (not even recorded as unresolved).
        for child in fd.node.children:
            _walk_body(child, fd.scope, fd.name, fd.line, scope_signals, calls, connections)

    for pa in iter_property_accessor_defs(tree):
        scope_signals = signal_names_by_scope.get(pa.scope, {})
        for child in pa.body:
            _walk_body(child, pa.scope, pa.name, pa.line, scope_signals, calls, connections)

    return calls, connections


def extract_calls(tree: Tree) -> list[RawCall]:
    calls, _ = extract_calls_and_connections(tree)
    return calls

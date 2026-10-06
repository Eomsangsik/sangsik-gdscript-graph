from __future__ import annotations

from dataclasses import dataclass

from lark import Token, Tree

from gdscript_graph.receivers import CHAINED_RECEIVER, Receiver, parse_getattr, receiver_with, split_getattr
from gdscript_graph.symbols import iter_function_defs, iter_property_accessor_defs

_CONNECT_METHOD = "connect"


@dataclass
class RawCall:
    caller_scope: str | None
    caller_function: str
    caller_line: int
    receiver: str | None  # None = standalone call; see CHAINED_RECEIVER for chains
    called_name: str
    line: int
    in_lambda: bool = False  # True if this call site is nested inside a lambda
    # The receiver in typeable form; None for a standalone call or a base
    # expression that can't be typed (e.g. `get_node("X").foo()`).
    receiver_expr: Receiver | None = None


@dataclass
class RawConnection:
    caller_scope: str | None
    caller_function: str
    line: int
    signal_name: str
    signal_receiver: str | None  # None = bare/self/inherited reference; otherwise the
                                  # receiver of a `<signal_receiver>.<signal_name>.connect(...)`
                                  # chain (`CHAINED_RECEIVER` unless a single name)
    handler_receiver: str | None  # None = handler in current scope (bare name or self.)
    handler_name: str | None  # None = handler argument shape isn't a simple name/getattr (e.g. a lambda)
    in_lambda: bool = False  # True if the .connect() call site is nested inside a lambda
    # Typeable forms of the two receivers (see `Receiver`) -- None when the
    # corresponding receiver is absent or can't be typed.
    signal_receiver_expr: Receiver | None = None
    handler_receiver_expr: Receiver | None = None


def _standalone_call_name(node: Tree) -> str | None:
    if not node.children:
        return None
    first = node.children[0]
    return str(first) if isinstance(first, Token) else None


def _parse_chained_signal_connect(getattr_tree: Tree) -> tuple[str, Receiver | None, str] | None:
    """For a getattr chain shaped `<receiver>.<signal_name>.connect` (e.g.
    `GameManager.card_drawn.connect(...)`, `self.hud.closed.connect(...)`,
    `$Timer.timeout.connect(...)`), return (display receiver, typeable
    receiver or None, signal_name). Returns None for any other shape -- a
    bare signal reference has no receiver segment to extract here (handled
    separately).

    Connecting to a signal accessed through some receiver is by far the
    most common real-world signal-wiring idiom. Whether `<receiver>`
    resolves to a type and whether `<signal_name>` is a project-declared
    signal on it are both worked out later during resolution, not here --
    the handler is a real registration either way (the signal may well be
    an engine built-in, e.g. `$Button.pressed`)."""
    split = split_getattr(getattr_tree)
    if split is None:
        return None
    base, names = split
    if len(names) < 2 or names[-1] != _CONNECT_METHOD:
        return None
    receiver = receiver_with(base, names[:-2])
    if receiver is None:
        return CHAINED_RECEIVER, None, names[-2]
    return receiver.display, receiver, names[-2]


def _names_a_signal(arg: object) -> bool:
    """Whether a `.connect()` call's first argument is a signal *name* (a
    string or `&"..."` literal) -- `Object.connect("signal", ...)`, or
    Godot 3's `connect("signal", target, "method")` -- rather than the
    handler of Godot 4's `<signal>.connect(<callable>)`."""
    return isinstance(arg, Tree) and arg.data in ("string", "string_name")


def _parse_callable_arg(arg: object) -> tuple[str | None, str | None, Receiver | None]:
    """Parse a bare handler reference passed to .connect(), e.g. `_on_died`,
    `self._on_died` or `menu.open`. Returns (display receiver, name,
    typeable receiver), or all None if the argument isn't a simple
    name/getattr reference (e.g. a lambda)."""
    if isinstance(arg, Token) and arg.type == "NAME":
        return None, str(arg), None
    if isinstance(arg, Tree) and arg.data == "getattr":
        return parse_getattr(arg)
    return None, None, None


def _walk_body(
    node: object,
    scope: str | None,
    caller_name: str,
    caller_line: int,
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
            # A signal connection is `.connect(<handler>)` -- unless the
            # first argument names the signal, which makes it a plain
            # `Object.connect(...)` call instead.
            has_arg = len(node.children) >= 2 and not _names_a_signal(node.children[1])
            chained_connect = _parse_chained_signal_connect(getattr_node) if has_arg else None
            receiver_expr: Receiver | None = None
            if chained_connect is not None and chained_connect[0] == "self":
                # `self.<signal>.connect(...)` means exactly the same thing
                # as bare `<signal>.connect(...)` -- route it through the
                # bare-form lookup below (which also reaches signals
                # inherited from an ancestor file, and inner-class-scoped
                # ones) instead of receiver-chain resolution.
                receiver, method = chained_connect[2], _CONNECT_METHOD
                receiver_expr = Receiver(display=receiver, chain=("self", receiver))
            elif chained_connect is not None:
                signal_receiver, signal_receiver_expr, signal_name = chained_connect
                handler_receiver, handler_name, handler_expr = _parse_callable_arg(node.children[1])
                connections.append(RawConnection(
                    caller_scope=scope,
                    caller_function=caller_name,
                    line=getattr(node.meta, "line", 0),
                    signal_name=signal_name,
                    signal_receiver=signal_receiver,
                    handler_receiver=handler_receiver,
                    handler_name=handler_name,
                    in_lambda=in_lambda,
                    signal_receiver_expr=signal_receiver_expr,
                    handler_receiver_expr=handler_expr,
                ))
                # Once recognized as a connect() site (even one whose
                # signal receiver might fail to resolve later), never also
                # record it as a generic call to a same-named method.
                receiver, method = None, None
            else:
                receiver, method, receiver_expr = parse_getattr(getattr_node)

            # A bare (or `self.`) `<signal>.connect(<handler>)` -- a
            # project-declared signal in scope (worked out in resolve.py),
            # or any other name: an engine built-in signal of this class
            # (`tree_exited.connect(_on_exit)`) or a Signal-typed variable.
            is_bare_connect = (
                method == _CONNECT_METHOD
                and has_arg
                and receiver_expr is not None
                and receiver_expr.cast is None
                and receiver_expr.chain in ((receiver,), ("self", receiver))
                and receiver not in ("self", "super")
            )
            if is_bare_connect:
                handler_receiver, handler_name, handler_expr = _parse_callable_arg(node.children[1])
                connections.append(RawConnection(
                    caller_scope=scope,
                    caller_function=caller_name,
                    line=getattr(node.meta, "line", 0),
                    signal_name=receiver,
                    signal_receiver=None,
                    handler_receiver=handler_receiver,
                    handler_name=handler_name,
                    in_lambda=in_lambda,
                    handler_receiver_expr=handler_expr,
                ))
                # Once recognized as a connect() site, never also record it
                # as a generic call -- even when the handler shape couldn't
                # be parsed (e.g. an inline lambda), it isn't a plain
                # "unresolved call to connect()".
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
                    receiver_expr=receiver_expr,
                ))

    # Recurse unconditionally so calls nested in arguments or in a chained
    # getattr base (e.g. get_node("X").foo()) are still found. Once inside
    # a lambda, every descendant call stays flagged in_lambda=True, even
    # across nested lambdas.
    child_in_lambda = in_lambda or node.data == "lambda"
    for child in node.children:
        _walk_body(child, scope, caller_name, caller_line, calls, connections, child_in_lambda)


def extract_calls_and_connections(tree: Tree) -> tuple[list[RawCall], list[RawConnection]]:
    """Extract call sites and `<signal>.connect(<handler>)` registrations,
    grouped by their enclosing function -- from this one file alone, so the
    result can be cached by the file's content.

    Only calls inside a function body are tracked -- calls in class-level
    var initializers (rare) are out of scope for v1. Two connection shapes
    are recognized: the simple `signal_name.connect(handler)` form (also
    `self.signal_name.connect(...)`), whose signal resolve.py looks up in
    the connect() call's own scope -- and, at top level, up the `extends`
    chain -- and `<receiver>.signal_name.connect(handler)`, the far more
    common real-world idiom (an autoload, `class_name`, or typed
    variable/field receiver, possibly through a chain of typed fields or a
    cast, e.g. `GameManager.card_drawn.connect(...)`,
    `self.hud.closed.connect(...)`), recognized here by shape and resolved
    later in `resolve.py` once the receiver's actual type is known.
    """
    calls: list[RawCall] = []
    connections: list[RawConnection] = []

    for fd in iter_function_defs(tree):
        # Walk children[0] (func_header) too, not just the body -- a
        # default-argument-value expression (`func f(x = some_call()):`)
        # can itself contain a call, which would otherwise vanish from the
        # graph entirely (not even recorded as unresolved).
        for child in fd.node.children:
            _walk_body(child, fd.scope, fd.name, fd.line, calls, connections)

    for pa in iter_property_accessor_defs(tree):
        for child in pa.body:
            _walk_body(child, pa.scope, pa.name, pa.line, calls, connections)

    return calls, connections


def extract_calls(tree: Tree) -> list[RawCall]:
    calls, _ = extract_calls_and_connections(tree)
    return calls

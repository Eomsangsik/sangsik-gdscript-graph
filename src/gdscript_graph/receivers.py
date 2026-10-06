from __future__ import annotations

from dataclasses import dataclass

from lark import Token, Tree

_NAME_TOKEN_TYPES = ("NAME", "GET", "SET")

# Display receiver for a getattr base that isn't a single name: either the
# base is itself an expression (e.g. get_node("X").foo()) or the attribute
# path has 2+ segments after the base (e.g. self.child.foo()). This string
# can never collide with a real GDScript identifier, so it can never be
# misattributed to a same-named autoload/class_name/local. Such a receiver
# only resolves through its `receiver_chain`/`receiver_cast` (a chain of
# typed fields, or an `(<expr> as T)` cast -- see resolve.py); otherwise it
# falls through as "unknown_receiver".
CHAINED_RECEIVER = "<chained>"


@dataclass(frozen=True)
class Receiver:
    """A receiver expression in a form resolve.py can type: an optional
    `(<expr> as T)` cast or call-result base, followed by plain name
    segments -- e.g. `self.hud.menu` -> (None, ("self", "hud", "menu")),
    `($X as T).menu` -> ("T", ("menu",)), `repo().menu` -> call `repo()`,
    ("menu",). `display` is what gets recorded for an unresolved call: the
    bare name for a single segment, else `CHAINED_RECEIVER`."""
    display: str
    chain: tuple[str, ...]
    cast: str | None = None
    call: CallType | None = None


@dataclass(frozen=True)
class CallType:
    """The type of a call's result -- `f()`, `obj.m()`, `Cls.make()` -- left
    for resolve.py to work out: the called function's declared return type,
    once the function itself is found (`receiver` None = a bare call)."""
    receiver: Receiver | None
    name: str


def call_type(node: object) -> CallType | None:
    """A `CallType` for a call expression (`f(...)` / `<receiver>.m(...)`),
    else None. `preload(...)` isn't a call to type this way."""
    if isinstance(node, Tree) and node.data == "standalone_call" and node.children:
        callee = node.children[0]
        if isinstance(callee, Token) and str(callee) != "preload":
            return CallType(None, str(callee))
        return None
    if isinstance(node, Tree) and node.data == "getattr_call" and node.children:
        getattr_node = node.children[0]
        if isinstance(getattr_node, Tree) and getattr_node.data == "getattr":
            _, method, receiver = parse_getattr(getattr_node)
            if method is not None and receiver is not None:
                return CallType(receiver, method)
    return None


def cast_type(node: object) -> str | None:
    """The target type of a parenthesized `(<expr> as T)`, else None."""
    if not (isinstance(node, Tree) and node.data == "par_expr" and len(node.children) == 1):
        return None
    cast = node.children[0]
    if not (isinstance(cast, Tree) and cast.data == "actual_type_cast" and cast.children):
        return None
    type_token = cast.children[-1]
    return str(type_token) if isinstance(type_token, Token) else None


def split_getattr(getattr_tree: Tree) -> tuple[Receiver | None, list[str]] | None:
    """Split a getattr chain into (its base, every attribute name after it).
    The base is a `Receiver` holding just the base itself (a name, a
    parenthesized cast, or a call); None when it's some other expression.
    Returns None for a malformed (empty) getattr."""
    children = getattr_tree.children
    if not children:
        return None
    base = children[0]
    names = [str(c) for c in children[1:] if isinstance(c, Token) and c.type in _NAME_TOKEN_TYPES]
    if isinstance(base, Token):
        return Receiver(display=str(base), chain=(str(base),)), names
    cast = cast_type(base)
    if cast is not None:
        return Receiver(display=CHAINED_RECEIVER, chain=(), cast=cast), names
    call = call_type(base)
    if call is not None:
        return Receiver(display=CHAINED_RECEIVER, chain=(), call=call), names
    return None, names


def receiver_with(base: Receiver | None, fields: list[str]) -> Receiver | None:
    """`base` followed by attribute `fields`, e.g. `a` + [b, c] -> `a.b.c`."""
    if base is None or not fields:
        return base
    return Receiver(display=CHAINED_RECEIVER, chain=base.chain + tuple(fields), cast=base.cast, call=base.call)


def parse_getattr(getattr_tree: Tree) -> tuple[str | None, str | None, Receiver | None]:
    """(display receiver, called/referenced name, typeable receiver) for a
    getattr chain `<receiver>.<name>`."""
    split = split_getattr(getattr_tree)
    if split is None:
        return None, None, None
    base, names = split
    method = names[-1] if names else None
    receiver = receiver_with(base, names[:-1])
    if receiver is None:
        return CHAINED_RECEIVER, method, None
    return receiver.display, method, receiver

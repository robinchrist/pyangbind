"""pybind-dataclass-dumb: the deliberately dumb variant of pybind-dataclass.

Frozen snapshot of pybind-dataclass before validation and default-value
support were added: pure structure and type hints, nothing else. Kept as
its own output format for consumers that want the guarantee that the
generated code can never raise on assignment and that an unset leaf is
always None.

An alternative output format to the classic ``pybind`` plugin. Instead of
pyangbind's dynamic class machinery (``YANGDynClass`` wrappers, properties
attached via ``__builtin__.property(...)`` at class-build time -- opaque to
IDEs and type checkers), this emits ordinary ``@dataclasses.dataclass``
definitions with real type annotations:

- container      -> nested dataclass, field with ``default_factory``
- list           -> nested dataclass for the entry, ``list[Entry]`` field
                    (keyed-ness is not enforced; the key leaves are ordinary
                    fields of the entry)
- leaf           -> ``<type> | None = None``
- leaf-list      -> ``list[<type>]`` field
- enumeration    -> ``typing.Literal['a', 'b', ...]``
- identityref    -> ``typing.Literal[...]`` of every identity derived from
                    the base, in both bare (``bgp``) and module-prefixed
                    (``frr-bgp:bgp``) spelling
- union          -> ``T1 | T2`` of the mapped member types
- choice/case    -> flattened into the parent (mutual exclusion of cases is
                    not enforced)

The output is deliberately dumb: no validation, no change tracking, no YANG
defaults applied (an unset leaf is always ``None``, so "render only what was
explicitly set" consumers can rely on falsiness), no serialization. It
depends on nothing but the standard library. ``config false`` subtrees are
skipped entirely -- this backend targets *config generation*, where
operational state has no place.

Nested classes mirror the YANG tree, so the generated code reads like the
model: it is immediately obvious which data a node contains, and IDEs
resolve every attribute statically.
"""

import keyword
import re

from pyang import plugin

# YANG built-in types that map 1:1 onto a Python scalar annotation.
_SCALAR_TYPE_MAP = {
    "int8": "int",
    "int16": "int",
    "int32": "int",
    "int64": "int",
    "uint8": "int",
    "uint16": "int",
    "uint32": "int",
    "uint64": "int",
    "string": "str",
    "boolean": "bool",
    # An `empty` leaf carries no value; presence is the information. `True`
    # (or any truthy assignment) marks it present, `None` absent.
    "empty": "bool",
    "binary": "bytes",
    "bits": "set[str]",
    "instance-identifier": "str",
}

_DATA_KEYWORDS = ("container", "list", "leaf", "leaf-list", "choice")


def pyang_plugin_init():
    plugin.register_plugin(PybindDataclassDumbPlugin())


class PybindDataclassDumbPlugin(plugin.PyangPlugin):
    def add_output_format(self, fmts):
        self.multiple_modules = True
        fmts["pybind-dataclass-dumb"] = self

    def emit(self, ctx, modules, fd):
        build_dataclasses(ctx, modules, fd)


def safe_name(yang_name):
    """YANG identifier -> Python attribute name (pyangbind-compatible)."""
    name = yang_name.replace("-", "_").replace(".", "_")
    if keyword.iskeyword(name):
        name += "_"
    return name


def class_name(yang_name):
    """YANG identifier -> Python class name (CamelCase)."""
    parts = [p for p in re.split(r"[-._]", yang_name) if p]
    name = "".join(p[:1].upper() + p[1:] for p in parts)
    if keyword.iskeyword(name):
        name += "_"
    return name


def _build_identity_values(ctx):
    """Map each identity statement -> sorted value spellings of everything
    transitively derived from it (bare and module-prefixed)."""
    identities = {}  # id(stmt) -> stmt, dedupes multiple ctx.modules entries
    for module in ctx.modules.values():
        for ident in module.search("identity"):
            identities[id(ident)] = ident

    direct_derived = {}  # id(base stmt) -> [derived stmts]
    for ident in identities.values():
        for base in ident.search("base"):
            target = getattr(base, "i_identity", None)
            if target is not None:
                direct_derived.setdefault(id(target), []).append(ident)

    def spellings(ident):
        prefix_stmt = ident.i_module.search_one("prefix")
        if prefix_stmt is not None:
            yield "%s:%s" % (prefix_stmt.arg, ident.arg)
        yield ident.arg

    values = {}
    for base_id in list(direct_derived):
        seen, out, stack = set(), set(), list(direct_derived.get(base_id, []))
        while stack:
            ident = stack.pop()
            if id(ident) in seen:
                continue
            seen.add(id(ident))
            out.update(spellings(ident))
            stack.extend(direct_derived.get(id(ident), []))
        values[base_id] = sorted(out)
    return values


class _Emitter:
    def __init__(self, ctx, identity_values):
        self.ctx = ctx
        self.identity_values = identity_values
        self.lines = []
        self.uses_decimal = False

    # ---- type mapping -------------------------------------------------

    def _resolve_typedef_chain(self, type_stmt):
        while getattr(type_stmt, "i_typedef", None) is not None:
            inner = type_stmt.i_typedef.search_one("type")
            if inner is None:
                break
            type_stmt = inner
        return type_stmt

    def annotation(self, type_stmt, node, depth=0):
        """Python annotation string for a YANG `type` statement.

        `node` is the leaf/leaf-list carrying the type (needed for the
        resolved leafref target pyang stores on the node, not the type).
        """
        if depth > 16:  # defensive: leafref chains can in theory loop
            return "str"
        t = self._resolve_typedef_chain(type_stmt)

        if t.arg in _SCALAR_TYPE_MAP:
            return _SCALAR_TYPE_MAP[t.arg]
        if t.arg == "decimal64":
            self.uses_decimal = True
            return "decimal.Decimal"
        if t.arg == "enumeration":
            values = [e.arg for e in t.search("enum")]
            return self._literal(values) if values else "str"
        if t.arg == "identityref":
            base = t.search_one("base")
            target = getattr(base, "i_identity", None) if base is not None else None
            values = self.identity_values.get(id(target), []) if target is not None else []
            return self._literal(values) if values else "str"
        if t.arg == "union":
            members = []
            for member in t.search("type"):
                mapped = self.annotation(member, node, depth + 1)
                for part in mapped.split(" | "):
                    if part not in members:
                        members.append(part)
            return " | ".join(members) if members else "str"
        if t.arg == "leafref":
            ptr = getattr(node, "i_leafref_ptr", None)
            if ptr is not None and depth == 0:
                target_leaf = ptr[0]
                target_type = target_leaf.search_one("type")
                if target_type is not None:
                    return self.annotation(target_type, target_leaf, depth + 1)
            return "str"
        # anything unrecognized degrades to str rather than failing codegen
        return "str"

    @staticmethod
    def _literal(values):
        return "typing.Literal[%s]" % ", ".join(repr(v) for v in values)

    # ---- tree walking ---------------------------------------------------

    @staticmethod
    def _data_children(stmt):
        """Config-true data children, with choice/case flattened away."""
        out = []
        for child in getattr(stmt, "i_children", []) or []:
            if child.keyword not in _DATA_KEYWORDS:
                continue
            if not getattr(child, "i_config", True):
                continue
            if child.keyword == "choice":
                for case in getattr(child, "i_children", []) or []:
                    out.extend(_Emitter._data_children(case))
            else:
                out.append(child)
        return out

    def _docstring(self, stmt, indent):
        desc_stmt = stmt.search_one("description")
        if desc_stmt is None:
            return
        text = desc_stmt.arg.replace("\\", "\\\\").replace('"""', r"\"\"\"")
        lines = text.splitlines() or [""]
        if len(lines) == 1 and not lines[0].endswith('"'):
            self.lines.append('%s"""%s"""' % (indent, lines[0]))
            return
        self.lines.append('%s"""%s' % (indent, lines[0]))
        for line in lines[1:]:
            self.lines.append(("%s%s" % (indent, line)).rstrip())
        self.lines.append('%s"""' % indent)

    def emit_node_class(self, stmt, cname, indent):
        """Emit a dataclass for a container / list entry / module."""
        self.lines.append("%s@dataclasses.dataclass" % indent)
        self.lines.append("%sclass %s:" % (indent, cname))
        body_indent = indent + "    "
        body_start = len(self.lines)
        self._docstring(stmt, body_indent)

        used_class_names = set()
        for child in self._data_children(stmt):
            fname = safe_name(child.arg)
            if child.keyword in ("container", "list"):
                child_cname = class_name(child.arg)
                while child_cname in used_class_names:
                    child_cname += "_"
                used_class_names.add(child_cname)
                self.emit_node_class(child, child_cname, body_indent)
                if child.keyword == "container":
                    self.lines.append(
                        "%s%s: %s = dataclasses.field(default_factory=%s)"
                        % (body_indent, fname, child_cname, child_cname)
                    )
                else:
                    self.lines.append(
                        "%s%s: list[%s] = dataclasses.field(default_factory=list)"
                        % (body_indent, fname, child_cname)
                    )
                self.lines.append("")
            elif child.keyword == "leaf":
                ann = self.annotation(child.search_one("type"), child)
                self.lines.append("%s%s: %s | None = None" % (body_indent, fname, ann))
            elif child.keyword == "leaf-list":
                ann = self.annotation(child.search_one("type"), child)
                self.lines.append(
                    "%s%s: list[%s] = dataclasses.field(default_factory=list)"
                    % (body_indent, fname, ann)
                )

        if len(self.lines) == body_start:
            self.lines.append("%spass" % body_indent)
        self.lines.append("")


def build_dataclasses(ctx, modules, fd):
    identity_values = _build_identity_values(ctx)

    emitter = _Emitter(ctx, identity_values)
    emitted_any = False
    for module in modules:
        if not _Emitter._data_children(module):
            continue
        emitted_any = True
        emitter.emit_node_class(module, class_name(module.arg), "")
    if not emitted_any:
        emitter.lines.append("# (none of the input modules define config data nodes)")

    header = [
        '"""Typed dataclass bindings generated by pyangbind (pybind-dataclass-dumb plugin).',
        "",
        "Source YANG modules: %s." % ", ".join(sorted(m.arg for m in modules)),
        "Do not edit by hand -- regenerate instead.",
        '"""',
        "",
        "from __future__ import annotations",
        "",
        "import dataclasses",
        "import typing",
    ]
    if emitter.uses_decimal:
        header.append("import decimal")
    header.extend(["", ""])

    fd.write("\n".join(header + emitter.lines))
    if not emitter.lines or emitter.lines[-1] != "":
        fd.write("\n")

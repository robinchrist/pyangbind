"""pybind-dataclass: emit YANG models as plain, fully type-hinted dataclasses.

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
- enumeration    -> ``typing.Literal['a', 'b', ...]``; when the enum comes
                    from a named ``typedef`` it is hoisted to a module-level
                    reusable ``type <TypedefName> = typing.Literal[...]`` alias
                    (PEP 695, hence Python >= 3.12) referenced by name;
                    inline anonymous enums stay inlined
- identityref    -> a module-level ``type <BaseIdentity> = typing.Literal[...]``
                    alias of every identity derived from the base, in both bare
                    (``bgp``) and module-prefixed (``frr-bgp:bgp``) spelling;
                    every leaf sharing the base identity references the one alias
- union          -> ``T1 | T2`` of the mapped member types
- bits           -> module-level reusable dataclass with one ``bool =
                    False`` field per YANG bit, truthy iff any bit is set;
                    hoisted (not nested) so it can also be a union member.
                    The YANG default is applied at the field via a factory,
                    not baked into the class, so one class is shared across
                    leaves with differing defaults
- choice/case    -> flattened into the parent (mutual exclusion of cases is
                    not enforced)

``config false`` subtrees are skipped entirely -- this backend targets
*config generation*, where operational state has no place. The output
depends on nothing but the standard library, and nested classes mirror the
YANG tree, so the generated code reads like the model: it is immediately
obvious which data a node contains, and IDEs resolve every attribute
statically.

Two features, both ON by default, each with a CLI opt-out flag:

Validation (disable with ``--no-dataclass-validation``)
    A small (stdlib-only) runtime is embedded in the generated module and
    every generated class validates values *on assignment* (including
    dataclass ``__init__`` keyword arguments): base-type checks, integer /
    decimal ranges, string lengths and patterns, enumeration and
    identityref value sets, bits names, union membership. Violations raise
    ``YangValidationError``. Leaf-list elements are validated when the
    list is assigned; in-place ``.append()`` is not intercepted.
    Structural rules (mandatory, list keys/uniqueness, when/must) are NOT
    checked. The ``__setattr__`` hook is defined under ``if not
    typing.TYPE_CHECKING`` so static checkers keep enforcing the declared
    field types instead of degrading to any-attribute-assignable.

Defaults (disable with ``--no-dataclass-defaults``)
    YANG ``default`` statements (from the leaf itself or its typedef
    chain) become dataclass field defaults instead of ``None``. Disable
    this when consumers need "unset leaf is always None" so that
    falsiness reliably means "not explicitly configured". Field ordering
    is never a problem either way: *every* generated field has a default
    (``None``, the YANG default, or a factory), so the dataclass
    "non-default field follows defaulted field" TypeError cannot occur.

The pre-validation/pre-defaults variant of this backend is preserved
verbatim as ``pybind-dataclass-dumb``.
"""

import keyword
import optparse
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
    # degenerate fallback only: a `bits` type with no `bit` statements.
    # Real bits (leaf or union member) become a module-level reusable
    # dataclass of bools instead (see _register_bits_class).
    "bits": "set[str]",
    "instance-identifier": "str",
}

# annotation -> _Check base tag used by the embedded validation runtime
_CHECK_BASE = {
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
    "empty": "bool",
    "binary": "bytes",
    "bits": "bits",
    "instance-identifier": "str",
    "decimal64": "decimal",
}

# Implicit range restrictions of the YANG integer built-ins, enforced even
# when the model spells no explicit `range` statement.
_INT_BUILTIN_RANGE = {
    "int8": (-(2**7), 2**7 - 1),
    "int16": (-(2**15), 2**15 - 1),
    "int32": (-(2**31), 2**31 - 1),
    "int64": (-(2**63), 2**63 - 1),
    "uint8": (0, 2**8 - 1),
    "uint16": (0, 2**16 - 1),
    "uint32": (0, 2**32 - 1),
    "uint64": (0, 2**64 - 1),
}

_DATA_KEYWORDS = ("container", "list", "leaf", "leaf-list", "choice")

# Runtime embedded at the top of the generated module unless
# --no-dataclass-validation is given. Stdlib-only, so the generated file
# stays dependency-free. _YangNode.__setattr__ is hidden from type checkers: a
# visible `__setattr__(self, name: str, value: object)` would make mypy and
# pyright accept assignment of anything to any attribute, destroying
# exactly the static guarantees this backend exists to provide.
_VALIDATION_RUNTIME = '''
class YangValidationError(ValueError):
    """A value assigned to a generated field violates its YANG type."""


@dataclasses.dataclass(frozen=True)
class _Check:
    """Value-level YANG type restrictions for one leaf/leaf-list field."""

    base: str
    # Range/length restrictions are tuples of *statements* (one per level
    # of the typedef chain -- every statement must hold), each statement a
    # tuple of (low, high) alternatives (at least one must hold; None
    # means unbounded).
    ranges: tuple = ()
    lengths: tuple = ()
    patterns: tuple = ()  # every pattern must fullmatch (YANG ANDs them)
    values: tuple = ()  # allowed enum/identityref strings; () = unrestricted
    bits: tuple = ()  # allowed bit names; () = unrestricted
    members: tuple = ()  # union member checks; at least one must pass

    def validate(self, value, path):
        if self.base == "union":
            for member in self.members:
                try:
                    member.validate(value, path)
                    return
                except YangValidationError:
                    continue
            raise YangValidationError(
                "%s: %r does not match any member type of the union" % (path, value)
            )
        self._check_base_type(value, path)
        if self.values and value not in self.values:
            raise YangValidationError(
                "%s: %r is not one of the allowed values %s" % (path, value, list(self.values))
            )
        for statement in self.ranges:
            if not any(
                (low is None or value >= low) and (high is None or value <= high)
                for low, high in statement
            ):
                raise YangValidationError(
                    "%s: %r is out of range %s" % (path, value, statement)
                )
        for statement in self.lengths:
            length = len(value)
            if not any(
                (low is None or length >= low) and (high is None or length <= high)
                for low, high in statement
            ):
                raise YangValidationError(
                    "%s: length %d is outside %s" % (path, length, statement)
                )
        for pattern in self.patterns:
            if re.fullmatch(pattern, value) is None:
                raise YangValidationError(
                    "%s: %r does not match pattern %r" % (path, value, pattern)
                )

    def _check_base_type(self, value, path):
        base = self.base
        if base == "int":
            ok = isinstance(value, int) and not isinstance(value, bool)
        elif base == "str":
            ok = isinstance(value, str)
        elif base == "bool":
            ok = isinstance(value, bool)
        elif base == "decimal":
            ok = isinstance(value, (int, float, decimal.Decimal)) and not isinstance(value, bool)
        elif base == "bytes":
            ok = isinstance(value, (bytes, bytearray))
        elif base == "bits":
            if isinstance(value, _YangNode):
                # A generated bits dataclass (e.g. a bits member of a union);
                # it validates its own bool fields on assignment.
                ok = True
            else:
                ok = isinstance(value, (set, frozenset)) and all(
                    isinstance(bit, str) for bit in value
                )
                if ok and self.bits:
                    unknown = sorted(set(value) - set(self.bits))
                    if unknown:
                        raise YangValidationError("%s: unknown bits %s" % (path, unknown))
        else:
            ok = True
        if not ok:
            raise YangValidationError(
                "%s: expected a %s-compatible value, got %r" % (path, base, value)
            )


class _YangNode:
    """Base of every generated dataclass: validates each assignment
    (dataclass __init__ assigns through __setattr__ too)."""

    _field_checks: typing.ClassVar[dict] = {}

    if not typing.TYPE_CHECKING:

        def __setattr__(self, name, value):
            check = self._field_checks.get(name)
            if check is not None and value is not None:
                path = "%s.%s" % (type(self).__name__, name)
                if isinstance(value, list):
                    for index, element in enumerate(value):
                        check.validate(element, "%s[%d]" % (path, index))
                else:
                    check.validate(value, path)
            object.__setattr__(self, name, value)
'''


def pyang_plugin_init():
    plugin.register_plugin(PybindDataclassPlugin())


class PybindDataclassPlugin(plugin.PyangPlugin):
    def add_output_format(self, fmts):
        self.multiple_modules = True
        fmts["pybind-dataclass"] = self

    def add_opts(self, optparser):
        group = optparse.OptionGroup(optparser, "pybind-dataclass output specific options")
        group.add_option(
            "--no-dataclass-validation",
            dest="no_dataclass_validation",
            action="store_true",
            default=False,
            help="Do not generate on-assignment validation of YANG type "
            "restrictions (ranges, lengths, patterns, enum/identity "
            "values, bits, unions) into the dataclasses "
            "(validation is generated by default)",
        )
        group.add_option(
            "--no-dataclass-defaults",
            dest="no_dataclass_defaults",
            action="store_true",
            default=False,
            help="Do not apply YANG 'default' statements as dataclass "
            "field defaults; every unset leaf is then None, so falsiness "
            "means 'not explicitly configured' "
            "(defaults are applied by default)",
        )
        optparser.add_option_group(group)

    def emit(self, ctx, modules, fd):
        build_dataclasses(
            ctx,
            modules,
            fd,
            with_validation=not getattr(ctx.opts, "no_dataclass_validation", False),
            with_defaults=not getattr(ctx.opts, "no_dataclass_defaults", False),
        )


def safe_name(yang_name):
    """YANG identifier -> Python attribute name (pyangbind-compatible)."""
    name = yang_name.replace("-", "_").replace(".", "_")
    if keyword.iskeyword(name):
        name += "_"
    return name


def class_name(yang_name):
    """YANG identifier -> Python class name (CamelCase). Any module prefix
    (``foo:bar``) is folded in like any other separator."""
    parts = [p for p in re.split(r"[-._:]", yang_name) if p]
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


def _parse_bound(text):
    text = text.strip()
    if text in ("min", "max"):
        return None
    try:
        return int(text)
    except ValueError:
        return float(text)


def _parse_range_arg(arg):
    """'1..20 | 100 | 200..max' -> ((1, 20), (100, 100), (200, None))."""
    parts = []
    for piece in arg.split("|"):
        piece = piece.strip()
        if ".." in piece:
            low_text, high_text = piece.split("..", 1)
            parts.append((_parse_bound(low_text), _parse_bound(high_text)))
        else:
            bound = _parse_bound(piece)
            parts.append((bound, bound))
    return tuple(parts)


class _Emitter:
    def __init__(self, ctx, identity_values, with_validation, with_defaults):
        self.ctx = ctx
        self.identity_values = identity_values
        self.with_validation = with_validation
        self.with_defaults = with_defaults
        self.lines = []
        self.uses_decimal = False
        # Module-level reusable types (Literal aliases and bits dataclasses),
        # emitted once between the imports/runtime and the tree classes and
        # referenced by name from every use site.
        self.reusable_lines = []
        self.reusable_by_key = {}  # dedup key -> assigned Python name
        self.reusable_names = set()  # every assigned name, for collision avoidance

    def _register_reusable(self, key, preferred_name, build_lines):
        """Register (once) a module-level reusable type. `key` dedups repeated
        requests for the same YANG type (e.g. an identity used by many leaves);
        `build_lines(name)` returns the source lines for the chosen name.
        Returns the Python name to reference it by."""
        existing = self.reusable_by_key.get(key)
        if existing is not None:
            return existing
        name = preferred_name
        while name in self.reusable_names:
            name += "_"
        self.reusable_names.add(name)
        self.reusable_by_key[key] = name
        self.reusable_lines.extend(build_lines(name))
        self.reusable_lines.append("")
        return name

    # ---- type resolution ------------------------------------------------

    def _typedef_chain(self, type_stmt):
        """The `type` statements from the one written on the node down to
        the YANG built-in, following typedefs. Restrictions may sit on any
        level; the last element names the base type."""
        chain = [type_stmt]
        t = type_stmt
        while getattr(t, "i_typedef", None) is not None:
            inner = t.i_typedef.search_one("type")
            if inner is None:
                break
            chain.append(inner)
            t = inner
        return chain

    def _resolve_typedef_chain(self, type_stmt):
        return self._typedef_chain(type_stmt)[-1]

    def annotation(self, type_stmt, node, depth=0):
        """Python annotation string for a YANG `type` statement.

        `node` is the leaf/leaf-list carrying the type (needed for the
        resolved leafref target pyang stores on the node, not the type).
        """
        if depth > 16:  # defensive: leafref chains can in theory loop
            return "str"
        t = self._resolve_typedef_chain(type_stmt)

        if t.arg == "bits" and t.search("bit"):
            # A module-level reusable bits dataclass (hoisted so it can be a
            # union member too), referenced by name.
            return self._register_bits_class(type_stmt, t, node)
        if t.arg in _SCALAR_TYPE_MAP:
            return _SCALAR_TYPE_MAP[t.arg]
        if t.arg == "decimal64":
            self.uses_decimal = True
            return "decimal.Decimal"
        if t.arg == "enumeration":
            values = [e.arg for e in t.search("enum")]
            if not values:
                return "str"
            # A named typedef gives a stable, reusable name; an inline
            # (anonymous) enumeration has none, so it stays inlined.
            typedef = getattr(type_stmt, "i_typedef", None)
            if typedef is not None:
                # typedef.arg is the bare typedef name; type_stmt.arg may be a
                # prefixed cross-module reference (e.g. "frr-bt:as-type").
                return self._register_literal_alias(
                    ("typedef", id(typedef)), class_name(typedef.arg), values
                )
            return self._literal(values)
        if t.arg == "identityref":
            values = self._identityref_values(t)
            if not values:
                return "str"
            # identityrefs always have a natural name: their base identity.
            # Every leaf sharing the base collapses onto one alias.
            base = t.search_one("base")
            identity = getattr(base, "i_identity", None) if base is not None else None
            if identity is not None:
                return self._register_literal_alias(
                    ("identity", id(identity)), class_name(identity.arg), values
                )
            return self._literal(values)
        if t.arg == "union":
            members = []
            for member in t.search("type"):
                mapped = self.annotation(member, node, depth + 1)
                for part in mapped.split(" | "):
                    if part not in members:
                        members.append(part)
            return " | ".join(members) if members else "str"
        if t.arg == "leafref":
            target = self._leafref_target(node, depth)
            if target is not None:
                return self.annotation(target.search_one("type"), target, depth + 1)
            return "str"
        # anything unrecognized degrades to str rather than failing codegen
        return "str"

    def _identityref_values(self, resolved_type_stmt):
        base = resolved_type_stmt.search_one("base")
        target = getattr(base, "i_identity", None) if base is not None else None
        if target is None:
            return []
        return self.identity_values.get(id(target), [])

    def _leafref_target(self, node, depth):
        """The leaf a leafref points at, when pyang resolved it (only
        recorded on the node itself, so only available at depth 0)."""
        if depth != 0:
            return None
        ptr = getattr(node, "i_leafref_ptr", None)
        if ptr is None or ptr[0].search_one("type") is None:
            return None
        return ptr[0]

    @staticmethod
    def _literal(values):
        return "typing.Literal[%s]" % ", ".join(repr(v) for v in values)

    def _register_literal_alias(self, key, preferred_name, values):
        """Emit `type <Name> = typing.Literal[...]` once at module level and
        return <Name>. A PEP 695 type alias (pure alias, never constructed),
        so the generated module requires Python >= 3.12."""
        literal = self._literal(values)
        return self._register_reusable(
            key, preferred_name, lambda name: ["type %s = %s" % (name, literal)]
        )

    # ---- validation check specs ----------------------------------------

    def check_expr(self, type_stmt, node, depth=0):
        """`_Check(...)` constructor source for this type, or None when
        nothing can be validated (which for a union member means it
        accepts anything, so the whole union check collapses to None)."""
        if depth > 16:
            return None
        chain = self._typedef_chain(type_stmt)
        base = chain[-1]

        ranges, lengths, patterns = [], [], []
        for level in chain:
            range_stmt = level.search_one("range")
            if range_stmt is not None:
                ranges.append(_parse_range_arg(range_stmt.arg))
            length_stmt = level.search_one("length")
            if length_stmt is not None:
                lengths.append(_parse_range_arg(length_stmt.arg))
            for pattern_stmt in level.search("pattern"):
                try:  # YANG uses XSD regexes; skip the rare Python-incompatible one
                    re.compile(pattern_stmt.arg)
                except re.error:
                    continue
                patterns.append(pattern_stmt.arg)

        values = ()
        bits = ()
        members = []
        if base.arg == "union":
            for member in base.search("type"):
                member_expr = self.check_expr(member, node, depth + 1)
                if member_expr is None:
                    return None  # one wide-open member makes the union wide open
                members.append(member_expr)
            if not members:
                return None
            check_base = "union"
        elif base.arg == "enumeration":
            enum_names = [e.arg for e in base.search("enum")]
            if not enum_names:
                return None
            values = tuple(enum_names)
            check_base = "str"
        elif base.arg == "identityref":
            identity_names = self._identityref_values(base)
            if not identity_names:
                return None
            values = tuple(identity_names)
            check_base = "str"
        elif base.arg == "leafref":
            target = self._leafref_target(node, depth)
            if target is None:
                return None
            return self.check_expr(target.search_one("type"), target, depth + 1)
        elif base.arg == "bits":
            bits = tuple(sorted(bit.arg for bit in base.search("bit")))
            check_base = "bits"
        elif base.arg in _CHECK_BASE:
            check_base = _CHECK_BASE[base.arg]
            builtin_range = _INT_BUILTIN_RANGE.get(base.arg)
            if builtin_range is not None:
                ranges.append((builtin_range,))
        else:
            return None

        args = [repr(check_base)]
        if ranges:
            args.append("ranges=%r" % (tuple(ranges),))
        if lengths:
            args.append("lengths=%r" % (tuple(lengths),))
        if patterns:
            args.append("patterns=%r" % (tuple(patterns),))
        if values:
            args.append("values=%r" % (values,))
        if bits:
            args.append("bits=%r" % (bits,))
        if members:
            args.append("members=(%s,)" % ", ".join(members))
        return "_Check(%s)" % ", ".join(args)

    # ---- YANG defaults ---------------------------------------------------

    def default_exprs(self, node, type_stmt):
        """Python source for a leaf/leaf-list's default, as a
        (scalar_expr, factory_expr) pair with exactly one non-None entry,
        or (None, None) when the node has no usable YANG default."""
        if node.keyword == "leaf-list":
            default_stmts = node.search("default")
            if not default_stmts:
                return None, None
            elements = [self._default_value_expr(d.arg, type_stmt) for d in default_stmts]
            return None, "lambda: [%s]" % ", ".join(elements)

        default_stmt = node.search_one("default")
        chain = self._typedef_chain(type_stmt)
        for level in chain:
            if default_stmt is not None:
                break
            typedef = getattr(level, "i_typedef", None)
            if typedef is not None:
                default_stmt = typedef.search_one("default")
        if default_stmt is None:
            return None, None

        # (bits leaves never get here -- they take the nested-dataclass
        # path in emit_node_class, where the default sets bit fields True)
        return self._default_value_expr(default_stmt.arg, type_stmt), None

    def _default_value_expr(self, text, type_stmt):
        base = self._resolve_typedef_chain(type_stmt)
        if base.arg in _INT_BUILTIN_RANGE:
            return repr(int(text, 0))
        if base.arg == "boolean":
            return repr(text.strip() == "true")
        if base.arg == "decimal64":
            self.uses_decimal = True
            return "decimal.Decimal(%r)" % text.strip()
        if base.arg == "union":
            try:
                return repr(int(text, 0))
            except ValueError:
                return repr(text)
        return repr(text)

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

    def _register_bits_class(self, type_stmt, resolved_type, node):
        """Register (once) a module-level dataclass for a bits type -- one
        bool field per YANG bit, typo-proof and autocompleted (unlike a
        set[str]), truthy iff any bit is set. Hoisted to module level (not
        nested) so it can also be a *union* member. The YANG default is
        applied at the field via a factory (see _bits_default_factory), not
        baked into the class, so one class is shared by every leaf of the
        type even when their defaults differ. Returns the class name."""
        typedef = getattr(type_stmt, "i_typedef", None)
        label = typedef.arg if typedef is not None else node.arg
        preferred = class_name(typedef.arg if typedef is not None else node.arg)
        bit_fnames = [safe_name(bit.arg) for bit in resolved_type.search("bit")]

        def build(name):
            base = "(_YangNode)" if self.with_validation else ""
            out = [
                "@dataclasses.dataclass",
                "class %s%s:" % (name, base),
                '    """Bits `%s`: one bool per YANG bit; truthy iff any bit is set."""'
                % label,
            ]
            out.extend("    %s: bool = False" % bit_fname for bit_fname in bit_fnames)
            if self.with_validation:
                out.append("")
                out.append("    _field_checks = {")
                out.extend("        %r: _Check('bool')," % bf for bf in bit_fnames)
                out.append("    }")
            out.append("")
            out.append("    def __bool__(self) -> bool:")
            out.append("        return any(vars(self).values())")
            return out

        # id(resolved_type) dedups across leaves sharing a typedef (they
        # resolve to the same inner `type bits` statement) while staying
        # unique per inline bits type.
        return self._register_reusable(("bits", id(resolved_type)), preferred, build)

    def _bits_default_factory(self, node, cname):
        """Field `default_factory` expr for a bits leaf: the class itself, or
        -- when defaults are enabled and a YANG default names bits -- a lambda
        constructing it with those bits set True."""
        if not self.with_defaults or node.keyword != "leaf":
            return cname
        default_stmt = node.search_one("default")
        for level in self._typedef_chain(node.search_one("type")):
            if default_stmt is not None:
                break
            typedef = getattr(level, "i_typedef", None)
            if typedef is not None:
                default_stmt = typedef.search_one("default")
        if default_stmt is None:
            return cname
        kwargs = ", ".join("%s=True" % safe_name(b) for b in default_stmt.arg.split())
        return "lambda: %s(%s)" % (cname, kwargs)

    def _emit_leaf(self, child, fname, indent):
        ann = self.annotation(child.search_one("type"), child)
        scalar_default, factory = (None, None)
        if self.with_defaults:
            scalar_default, factory = self.default_exprs(child, child.search_one("type"))

        if child.keyword == "leaf-list":
            self.lines.append(
                "%s%s: list[%s] = dataclasses.field(default_factory=%s)"
                % (indent, fname, ann, factory or "list")
            )
        elif factory is not None:
            self.lines.append(
                "%s%s: %s | None = dataclasses.field(default_factory=%s)"
                % (indent, fname, ann, factory)
            )
        else:
            self.lines.append(
                "%s%s: %s | None = %s" % (indent, fname, ann, scalar_default or "None")
            )

    def emit_node_class(self, stmt, cname, indent):
        """Emit a dataclass for a container / list entry / module."""
        self.lines.append("%s@dataclasses.dataclass" % indent)
        base = "(_YangNode)" if self.with_validation else ""
        self.lines.append("%sclass %s%s:" % (indent, cname, base))
        body_indent = indent + "    "
        body_start = len(self.lines)
        self._docstring(stmt, body_indent)

        used_class_names = set()
        field_checks = []
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
            else:  # leaf / leaf-list
                resolved = self._resolve_typedef_chain(child.search_one("type"))
                if resolved.arg == "bits" and resolved.search("bit"):
                    cname = self._register_bits_class(
                        child.search_one("type"), resolved, child
                    )
                    if child.keyword == "leaf-list":
                        self.lines.append(
                            "%s%s: list[%s] = dataclasses.field(default_factory=list)"
                            % (body_indent, fname, cname)
                        )
                    else:
                        factory = self._bits_default_factory(child, cname)
                        self.lines.append(
                            "%s%s: %s = dataclasses.field(default_factory=%s)"
                            % (body_indent, fname, cname, factory)
                        )
                    self.lines.append("")
                    continue
                self._emit_leaf(child, fname, body_indent)
                if self.with_validation:
                    check = self.check_expr(child.search_one("type"), child)
                    if check is not None:
                        field_checks.append((fname, check))

        if field_checks:
            self.lines.append("")
            self.lines.append("%s_field_checks = {" % body_indent)
            for fname, check in field_checks:
                self.lines.append("%s    %r: %s," % (body_indent, fname, check))
            self.lines.append("%s}" % body_indent)

        if len(self.lines) == body_start:
            self.lines.append("%spass" % body_indent)
        self.lines.append("")


def build_dataclasses(ctx, modules, fd, with_validation=True, with_defaults=True):
    identity_values = _build_identity_values(ctx)

    emitter = _Emitter(ctx, identity_values, with_validation, with_defaults)
    # Reserve the top-level module class names so a reusable type (emitted in
    # the same module namespace) can never shadow one of them.
    emitter.reusable_names.update(class_name(m.arg) for m in modules)
    emitted_any = False
    for module in modules:
        if not _Emitter._data_children(module):
            continue
        emitted_any = True
        emitter.emit_node_class(module, class_name(module.arg), "")
    if not emitted_any:
        emitter.lines.append("# (none of the input modules define config data nodes)")

    header = [
        '"""Typed dataclass bindings generated by pyangbind (pybind-dataclass plugin).',
        "",
        "Source YANG modules: %s." % ", ".join(sorted(m.arg for m in modules)),
        "Generated with: validation=%s, defaults=%s." % (with_validation, with_defaults),
        "Do not edit by hand -- regenerate instead.",
        '"""',
        "",
        "from __future__ import annotations",
        "",
        "import dataclasses",
        "import typing",
    ]
    if with_validation:
        header.append("import re")
    if emitter.uses_decimal or with_validation:
        header.append("import decimal")
    if with_validation:
        header.append(_VALIDATION_RUNTIME)
    header.extend(["", ""])

    # Reusable types (Literal aliases, bits dataclasses) go between the
    # imports/runtime and the tree classes so forward references resolve and
    # bits classes exist before they are used as field factory defaults.
    body = emitter.reusable_lines + emitter.lines
    fd.write("\n".join(header + body))
    if not body or body[-1] != "":
        fd.write("\n")

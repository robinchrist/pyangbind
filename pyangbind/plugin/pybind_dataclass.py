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

Three features, all ON by default, each with a CLI opt-out flag:

Validation (disable with ``--no-dataclass-validation``)
    A small (stdlib-only) runtime is embedded in the generated module and
    every generated class validates values *on assignment* (including
    dataclass ``__init__`` keyword arguments): base-type checks, integer /
    decimal ranges (incl. decimal64 fraction-digits), string lengths and
    patterns, enumeration and identityref value sets, bits names, union
    membership. Violations raise ``YangValidationError``. Leaf-list
    elements are validated when the list is assigned; in-place
    ``.append()`` is not intercepted (but is caught by ``validate_tree``).
    The ``__setattr__`` hook is defined under ``if not
    typing.TYPE_CHECKING`` so static checkers keep enforcing the declared
    field types instead of degrading to any-attribute-assignable.

    Structural and referential rules cannot be checked on assignment (a
    leafref target may legitimately not exist *yet*; mandatory/keys can
    only be judged on a finished tree), so the module also gets a
    ``validate_tree(*roots)`` function for one final whole-tree pass:
    re-validates every value, then checks leafref referential integrity
    (across all passed roots), mandatory leaves, list keys present and
    unique, ``unique`` groups, leaf-list value uniqueness, min-/max-
    elements, choice exclusivity / mandatory choices, and YANG ``must``
    / ``when`` constraints. All violations are collected and raised as
    one ``YangValidationError``.

    ``must``/``when`` are evaluated by an XPath 1.0 subset engine
    embedded in the generated runtime (location paths incl. ``..`` /
    ``//`` / ``*``, predicates, comparisons with node-set semantics,
    arithmetic, and/or, the core functions plus ``current()`` and an
    exact-match ``derived-from-or-self()``). ``when`` context nodes
    follow RFC 7950 7.21.5 (direct: the node itself; inherited from
    uses/augment/choice/case: the parent), and prefixes are normalized
    to module names at codegen. Expressions outside the subset --
    explicit axes, variables, unimplemented functions, references to
    modules not part of the codegen run, or identity derivation beyond
    an exact match -- are skipped, never misjudged: filtered out of the
    metadata at codegen where detectable, otherwise skipped at
    evaluation time. Absolute paths resolve across all roots passed to
    ``validate_tree``, so cross-module constraints require passing every
    module root involved. Disable with ``--no-dataclass-must-when``.

Defaults (disable with ``--no-dataclass-defaults``)
    YANG ``default`` statements (from the leaf itself or its typedef
    chain) become dataclass field defaults instead of ``None``. Disable
    this when consumers need "unset leaf is always None" so that
    falsiness reliably means "not explicitly configured". Field ordering
    is never a problem either way: *every* generated field has a default
    (``None``, the YANG default, or a factory), so the dataclass
    "non-default field follows defaulted field" TypeError cannot occur.

Native IP types (disable with ``--no-dataclass-native-ip-types``)
    Leaves whose typedef chain passes through one of the RFC 6991
    ietf-inet-types address/prefix typedefs (``ip-address``,
    ``ipv4-address``, ``ipv6-address``, their ``-no-zone`` variants,
    ``ip-prefix``, ``ipv4-prefix``, ``ipv6-prefix``) are typed as the
    stdlib ``ipaddress`` classes (``IPv4Address`` / ``IPv6Address`` /
    ``IPv4Network`` / ``IPv6Network``, the ``ip-*`` unions as ``T4 |
    T6``) instead of pattern-checked strings. Validation checks the
    class instead of the patterns (for the IPv6 ``-no-zone`` variants
    also the absence of a ``scope_id``), YANG defaults construct the
    object, and serde encodes via ``str()`` and decodes via
    ``ipaddress.ip_address()`` / ``ip_network(..., strict=False)``. A
    union mixing native members with other types decodes by trying the
    native parses and keeping the first candidate the union check
    accepts, falling back to the plain string for the other members.
    Caveat: RFC 6991 permits a zone index on IPv4 addresses too, which
    ``ipaddress.IPv4Address`` cannot represent (IPv6 zones map onto
    ``IPv6Address.scope_id``) -- zoned IPv4 values are inexpressible
    with native types; disable the flag if you need them.

    The mapping is extensible to schema-defined typedefs with the
    repeatable ``--dataclass-native-type [MODULE:]TYPEDEF=CLASS[,...]``
    option -- a generator-only concern, never YANG metadata: the schema
    defines an ordinary (typically pattern-restricted string) typedef
    and the *generator invocation* names the Python class(es) its
    canonical string form round-trips through (constructor accepts the
    string, ``str()`` produces it back). E.g.
    ``--dataclass-native-type my-types:ip-address-and-prefix=ipaddress.IPv4Interface,ipaddress.IPv6Interface``
    for an ADDR/PREFIXLEN typedef -- the one interface-address shape
    RFC 6991 has no typedef for. The generated field is annotated with
    those classes (several = union), validated by isinstance, defaults
    construct the class, and serde encodes ``str()`` / decodes by
    trying each constructor. A mapping takes precedence over the
    built-in ietf-inet-types table and is honored regardless of
    ``--no-dataclass-native-ip-types`` (it was requested explicitly).

Whenever any feature needs it (currently: validation), every generated
class additionally carries schema metadata in ClassVars -- invisible to
``__init__``/``repr``/``==`` and to type-checker field lists:

- ``_yang_fields`` -- dict of field name -> ``_FieldMeta`` (original YANG
  name, defining module, node kind, nested class reference, value check,
  IETF-JSON encoding tag, structural flags such as mandatory / list keys /
  unique / min- and max-elements, resolved leafref target path,
  choice/case membership)
- ``_yang_name`` / ``_yang_module`` -- the class's own YANG identity
- ``_yang_choices`` -- choice name -> (mandatory flag, chain of
  enclosing (choice, case) pairs), for the choices
  flattened into the class

Serialisation (opt-in with ``--dataclass-serde``)
    Generates ``to_ietf_json(root)`` / ``from_ietf_json(cls, data)``
    module functions implementing RFC 7951 (JSON encoding of YANG data)
    over plain dicts: member names module-qualified at the top level and
    at module boundaries, 64-bit integers and decimal64 as JSON strings,
    ``empty`` as ``[null]``, binary as base64, bits as the space-joined
    set-bit names, identityrefs canonicalised to ``module-name:identity``.
    Decoding accepts qualified and bare member names and coerces the
    string encodings back; values flow through normal assignment, so
    on-assignment validation applies when generated. Limitations: a
    present-but-empty presence container does not round-trip (the
    dataclass shape cannot express container absence), and union members
    are encoded by their Python value type (a 64-bit integer union member
    is emitted as a JSON number).

Metadata annotations (disable with ``--no-dataclass-annotations``)
    RFC 7952 ``md:annotation`` statements found anywhere in the
    compilation set are registered (``_YANG_ANNOTATIONS``), and instances
    can carry annotation values as metadata -- invisible to
    ``__init__``/``repr``/``==``, stored in a per-instance
    ``_yang_metadata`` dict. ``annotate(node, comment="...")`` annotates
    the node itself (container / list entry / module root);
    ``annotate(node, "leaf_field", comment="...")`` a scalar leaf member
    (container/list members are annotated on the child object itself);
    ``annotate(node, "leaflist_field", i, comment="...")`` the i-th
    leaf-list entry (RFC 7952: annotations attach to individual entries,
    never a whole leaf-list). ``annotations(...)`` with the same
    addressing reads them back. Values are validated against the
    annotation's YANG type when validation is generated, and serde
    round-trips them per RFC 7952 section 5.2: a ``"@"`` metadata-object
    member inside container/list-entry objects, a sibling ``"@member"``
    object for leaves, a null-padded ``"@member"`` array for leaf-list
    entries, annotation names always ``module-name:name``-qualified.

XPaths (opt-in with ``--dataclass-xpaths``)
    Every generated class gets its absolute schema path as a
    ``_yang_schema_path`` ClassVar (module-name-qualified at the top and
    at module boundaries, matching FRR northbound convention), and the
    module gets ``data_path(root, node)`` computing the *instance* path
    of a container / list-entry / bits node with ``[key=value]``
    predicates -- root-relative, since the dataclasses deliberately carry
    no parent pointers. (PEP 750 t-strings were evaluated and rejected:
    their interpolations evaluate eagerly at literal-creation time, so a
    class-level template cannot defer to instance state.)

Multi-file output (opt-in with ``--dataclass-split-dir DIR``)
    Writes the bindings as a Python package under DIR instead of one
    (potentially huge) module on ``-o``/stdout: one file per YANG module
    that defines data nodes, holding only that module's tree classes.
    Nothing shared is duplicated -- the embedded runtime is generated
    once into ``_runtime.py`` and every reusable type (Literal aliases,
    bits dataclasses, identityref spelling maps) once into ``_types.py``
    -- and ``__init__.py`` re-exports everything, so ``import <package>``
    exposes exactly the same names as a single-file build. Per-module
    files import from ``._runtime``/``._types`` only, never from each
    other, so no import cycles can arise however the modules augment one
    another. (The classic pybind backend's ``--split-class-dir`` gets
    deduplication for free only because its runtime lives in the
    ``pyangbind.lib`` runtime dependency; this backend stays generated
    and stdlib-only.)

Origin comments (opt-in with ``--dataclass-origin-comments``)
    Augment-heavy schemas (FRR's, for one) make it genuinely hard to see
    where a generated node comes from. With this flag, every class/field
    contributed via a grouping (``uses``) or an ``augment`` gets a comment
    on the line above naming the defining file:line and the uses/augment
    site. Locally-defined nodes stay uncommented.

The pre-validation/pre-defaults variant of this backend is preserved
verbatim as ``pybind-dataclass-dumb``.
"""

import base64
import binascii
import importlib
import keyword
import optparse
import os.path
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
    "instance-identifier": "instance-identifier",
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

# RFC 6991 ietf-inet-types address/prefix typedefs mapped onto the stdlib
# `ipaddress` module (skipped with --no-dataclass-native-ip-types), as
# (annotation, _Check base tags, IETF-JSON encode tag): several check tags
# form a union of alternatives. IPv6 zone indexes ride on
# IPv6Address.scope_id (absent on the -no-zone variants); IPv4 zone
# indexes, which RFC 6991 also permits, have no stdlib representation --
# see the module docstring.
_INET_NATIVE_TYPES = {
    "ip-address": (
        "ipaddress.IPv4Address | ipaddress.IPv6Address",
        ("ipv4-address", "ipv6-address"),
        "ip-address",
    ),
    "ipv4-address": ("ipaddress.IPv4Address", ("ipv4-address",), "ip-address"),
    "ipv6-address": ("ipaddress.IPv6Address", ("ipv6-address",), "ip-address"),
    "ip-address-no-zone": (
        "ipaddress.IPv4Address | ipaddress.IPv6Address",
        ("ipv4-address", "ipv6-address-no-zone"),
        "ip-address",
    ),
    "ipv4-address-no-zone": ("ipaddress.IPv4Address", ("ipv4-address",), "ip-address"),
    "ipv6-address-no-zone": (
        "ipaddress.IPv6Address",
        ("ipv6-address-no-zone",),
        "ip-address",
    ),
    "ip-prefix": (
        "ipaddress.IPv4Network | ipaddress.IPv6Network",
        ("ipv4-prefix", "ipv6-prefix"),
        "ip-prefix",
    ),
    "ipv4-prefix": ("ipaddress.IPv4Network", ("ipv4-prefix",), "ip-prefix"),
    "ipv6-prefix": ("ipaddress.IPv6Network", ("ipv6-prefix",), "ip-prefix"),
}


def _parse_native_type_hints(specs):
    """--dataclass-native-type values -> {(module-or-None, typedef):
    (class paths,)}. Each spec is [MODULE:]TYPEDEF=CLASS[,CLASS...];
    without MODULE the typedef name matches in any module."""
    hints = {}
    for spec in specs or ():
        name, sep, classes = spec.partition("=")
        paths = tuple(c.strip() for c in classes.split(",") if c.strip())
        if not sep or not paths:
            raise ValueError(
                "--dataclass-native-type expects [MODULE:]TYPEDEF=CLASS[,CLASS...],"
                " got %r" % spec
            )
        module, _, typedef = name.strip().rpartition(":")
        hints[(module or None, typedef)] = paths
    return hints

_DATA_KEYWORDS = ("container", "list", "leaf", "leaf-list", "choice")

# XPath functions the embedded evaluator implements. must/when expressions
# calling anything else are not emitted into the metadata at all (the
# generated evaluator would raise _XPathUnsupported and skip them anyway;
# filtering here keeps the generated tables honest about what is checked).
_XPATH_SUPPORTED_FUNCTIONS = frozenset(
    {
        "boolean",
        "concat",
        "contains",
        "count",
        "current",
        "derived-from",
        "derived-from-or-self",
        "false",
        "not",
        "number",
        "re-match",
        "starts-with",
        "string",
        "string-length",
        "true",
    }
)

# Runtime embedded whenever any generated feature needs per-field schema
# metadata (validation today; serialisation and xpaths reuse the same
# table). Stdlib-only, like everything embedded in the generated module.
_META_RUNTIME = '''
@dataclasses.dataclass(frozen=True)
class _FieldMeta:
    """Schema metadata for one generated field (a leaf / leaf-list /
    container / list, or one bool bit of a bits dataclass)."""

    yang_name: str
    module: str  # module that contributed the node (RFC 7951 qualifier)
    kind: str  # "leaf" | "leaf-list" | "container" | "list" | "bit"
    cls: type | None = None  # nested/bits class of container/list/bits fields
    check: typing.Any = None  # _Check, when validation is generated
    encode: str | None = None  # IETF-JSON value encoding tag, when special
    identity_map: typing.Any = None  # identityref spelling -> RFC 7951 canonical
    mandatory: bool = False
    presence: bool = False  # presence container
    min_elements: int | None = None
    max_elements: int | None = None
    keys: tuple = ()  # list key field names
    # list `unique` groups: each group is a tuple of leaf paths, each
    # path a tuple of field-name steps (descendant containers + leaf)
    unique: tuple = ()
    leafref: str | None = None  # absolute schema path of the leafref target
    # chain of (choice, case) pairs a choice-flattened field came out
    # of, outermost first; None for fields outside any choice
    case: tuple | None = None
    natives: tuple = ()  # native Python classes (native-type hints), for serde
    # XPath constraints, evaluated by validate_tree where the node exists:
    musts: tuple = ()  # ((expression, error-message | None), ...)
    whens: tuple = ()  # ((expression, context-is-self), ...); False = parent


def _qualified_name(meta, parent_module):
    return (
        meta.yang_name
        if meta.module == parent_module
        else "%s:%s" % (meta.module, meta.yang_name)
    )


def _instance_key(entry, meta):
    """`key=value` text of a list entry's key leaves, for instance paths."""
    if not meta.keys:
        return None
    entry_fields = getattr(type(entry), "_yang_fields", {})
    parts = []
    for key in meta.keys:
        key_meta = entry_fields.get(key)
        yang_key = key_meta.yang_name if key_meta is not None else key
        parts.append("%s=%r" % (yang_key, getattr(entry, key, None)))
    return ",".join(parts)


@dataclasses.dataclass(frozen=True)
class _AnnotationDef:
    """One RFC 7952 metadata annotation (md:annotation) defined by the
    compiled schema set."""

    yang_name: str
    module: str  # defining module name (the RFC 7952 qualifier)
    check: typing.Any = None  # _Check, when validation is generated
    encode: str | None = None  # IETF-JSON value encoding tag, when special


# python-safe annotation name -> _AnnotationDef. Populated by generated
# code when the compiled modules define md:annotation statements (and
# --no-dataclass-annotations was not given); empty otherwise, in which
# case annotate() rejects every annotation name.
_YANG_ANNOTATIONS: dict = {}

# canonical `module-name:identity` -> tuple of DIRECT base canonicals.
# Populated by generated code; the XPath engine climbs it to evaluate
# derived-from() / derived-from-or-self() transitively.
_YANG_IDENTITY_BASES: dict = {}


def annotate(node, member=None, index=None, /, **values):
    """Attach RFC 7952 metadata annotations to a data node instance.

    ``annotate(node, comment="...")`` annotates the node itself (a
    container / list entry / module root). ``annotate(node, "field",
    comment="...")`` annotates the scalar leaf member ``field`` of
    ``node`` -- a container or list member is annotated on the child
    object itself, never through its parent. ``annotate(node, "field",
    i, comment="...")`` annotates the i-th entry of leaf-list member
    ``field`` (RFC 7952: annotations attach to individual leaf-list
    entries, never the whole leaf-list). Keyword names are the
    python-safe annotation names; a value of ``None`` removes that
    annotation. Values are validated against the annotation's YANG
    type when validation is generated. Returns ``node``."""
    fields = getattr(type(node), "_yang_fields", {})
    if member is None:
        if index is not None:
            raise ValueError("index given without a leaf-list member")
        key = None
    else:
        meta = fields.get(member)
        if meta is None:
            raise ValueError("%s has no member %r" % (type(node).__name__, member))
        if meta.kind == "leaf":
            if index is not None:
                raise ValueError(
                    "%r is a leaf; an entry index applies only to leaf-list members"
                    % (member,)
                )
            key = member
        elif meta.kind == "leaf-list":
            if index is None:
                raise ValueError(
                    "leaf-list member %r needs an entry index (RFC 7952: "
                    "annotations attach to individual leaf-list entries)" % (member,)
                )
            key = (member, index)
        else:
            raise ValueError(
                "%r is a %s member; annotate the child object itself"
                % (member, meta.kind)
            )
    store = getattr(node, "_yang_metadata", None)
    if store is None:
        store = {}
        object.__setattr__(node, "_yang_metadata", store)
    entry = store.setdefault(key, {})
    for name, value in values.items():
        adef = _YANG_ANNOTATIONS.get(name)
        if adef is None:
            raise ValueError(
                "no annotation named %r is defined by the compiled modules" % (name,)
            )
        if value is None:
            entry.pop(name, None)
        elif adef.check is not None:
            adef.check.validate(value, "@%s:%s" % (adef.module, adef.yang_name))
            entry[name] = value
        else:
            entry[name] = value
    if not entry:
        store.pop(key, None)
    return node


def annotations(node, member=None, index=None, /):
    """The RFC 7952 annotations attached to a node instance, one of its
    leaf members, or one leaf-list member entry (same addressing as
    annotate()), as a dict of python-safe annotation name -> value.
    Returns {} when there are none."""
    store = getattr(node, "_yang_metadata", None) or {}
    key = member if index is None else (member, index)
    return dict(store.get(key) or {})
'''

# XPath evaluator embedded alongside the validation runtime: evaluates the
# YANG `must`/`when` expressions collected into _FieldMeta. A deliberate
# XPath 1.0 subset (location paths incl. `..`/`//`/`*`, predicates,
# comparisons, arithmetic, and/or, the core functions plus current() and
# an exact-match derived-from-or-self()); expressions outside the subset
# are filtered at codegen, and anything that still escapes evaluation
# raises _XPathUnsupported at runtime and the constraint is skipped --
# never misjudged. Stdlib-only, like everything embedded.
_XPATH_EVAL_RUNTIME = r'''
class _XPathUnsupported(Exception):
    """The expression needs XPath outside the evaluated subset; the
    surrounding must/when check is skipped rather than misjudged."""


class _XNode:
    """Instance node for XPath evaluation: a container / list entry, a
    leaf value, or the document root joining all validate_tree roots.
    Carries the parent pointer the plain dataclasses deliberately
    don't."""

    __slots__ = ("obj", "meta", "parent", "value", "is_leaf", "roots")

    def __init__(self, obj, meta, parent, value=None, is_leaf=False, roots=None):
        self.obj = obj
        self.meta = meta
        self.parent = parent
        self.value = value
        self.is_leaf = is_leaf
        self.roots = roots  # document root only


def _xnode_has_data(obj):
    """YANG data-tree existence of a container instance. The bindings
    auto-instantiate every container object, so holding data is the only
    existence signal -- for presence containers too (present-but-empty
    is not representable; an untouched presence container is absent)."""
    for fname, meta in getattr(type(obj), "_yang_fields", {}).items():
        value = getattr(obj, fname, None)
        if value is None:
            continue
        if meta.kind == "container":
            if _xnode_has_data(value):
                return True
        elif isinstance(value, list):
            if value:
                return True
        elif meta.cls is not None and isinstance(value, meta.cls):
            if value:  # bits: exists iff any bit is set
                return True
        else:
            return True
    return False


def _xnode_children(xnode):
    if xnode.is_leaf:
        return
    holders = xnode.roots if xnode.roots is not None else [xnode.obj]
    for holder in holders:
        for fname, meta in getattr(type(holder), "_yang_fields", {}).items():
            value = getattr(holder, fname, None)
            if meta.kind == "container":
                if value is not None and _xnode_has_data(value):
                    yield _XNode(value, meta, xnode)
            elif meta.kind == "list":
                for entry in value or []:
                    yield _XNode(entry, meta, xnode)
            elif meta.kind == "leaf-list":
                for element in value or []:
                    yield _XNode(None, meta, xnode, value=element, is_leaf=True)
            elif meta.cls is not None and isinstance(value, meta.cls):
                if value:
                    yield _XNode(None, meta, xnode, value=value, is_leaf=True)
            elif value is not None:
                yield _XNode(None, meta, xnode, value=value, is_leaf=True)


def _xnode_string(xnode):
    """XPath string-value of a node. Only leaves have one here: the
    string-value of an interior node (concatenated descendant text) is
    never useful in YANG constraints and is not modelled."""
    if not xnode.is_leaf:
        raise _XPathUnsupported("string-value of a non-leaf node")
    meta, value = xnode.meta, xnode.value
    if meta is not None and meta.encode == "empty":
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if meta is not None and meta.cls is not None and isinstance(value, meta.cls):
        table = getattr(meta.cls, "_yang_fields", {})
        return " ".join(m.yang_name for f, m in table.items() if getattr(value, f))
    if isinstance(value, (set, frozenset)):
        return " ".join(sorted(value))
    if isinstance(value, float):
        return _xpath_string(value)
    return str(value)


def _xnode_equals_string(xnode, text):
    value = _xnode_string(xnode)
    if xnode.meta is not None and xnode.meta.encode == "identityref":
        # identityref spellings vary (bare / prefixed / module-qualified)
        # and the bindings accept several -- compare local names
        return value.split(":")[-1] == text.split(":")[-1]
    return value == text


_XPATH_TOKEN_RE = re.compile(
    r"""
      (?P<num>\d+(?:\.\d+)?|\.\d+)
    | (?P<lit>"[^"]*"|'[^']*')
    | (?P<op><=|>=|!=|//|[()\[\]|=<>+,*/-])
    | (?P<dots>\.\.|\.)
    | (?P<name>[A-Za-z_][\w.-]*(?::[A-Za-z_][\w.-]*)?)
    """,
    re.VERBOSE,
)


def _xpath_tokenize(text):
    tokens, pos = [], 0
    while pos < len(text):
        if text[pos].isspace():
            pos += 1
            continue
        match = _XPATH_TOKEN_RE.match(text, pos)
        if match is None:
            raise _XPathUnsupported("cannot tokenize %r" % text[pos:])
        tokens.append((match.lastgroup, match.group()))
        pos = match.end()
    return tokens


class _XPathParser:
    """Recursive-descent XPath 1.0 (subset) parser producing a
    nested-tuple AST. Anything outside the subset raises
    _XPathUnsupported."""

    def __init__(self, tokens):
        self.tokens = tokens
        self.pos = 0

    def peek(self):
        return self.tokens[self.pos] if self.pos < len(self.tokens) else (None, None)

    def advance(self):
        token = self.peek()
        self.pos += 1
        return token

    def expect_op(self, text):
        kind, value = self.advance()
        if kind != "op" or value != text:
            raise _XPathUnsupported("expected %r, got %r" % (text, value))

    def parse(self):
        ast = self.parse_or()
        if self.pos != len(self.tokens):
            raise _XPathUnsupported("trailing tokens %r" % (self.tokens[self.pos:],))
        return ast

    def parse_or(self):
        left = self.parse_and()
        while self.peek() == ("name", "or"):
            self.advance()
            left = ("or", left, self.parse_and())
        return left

    def parse_and(self):
        left = self.parse_equality()
        while self.peek() == ("name", "and"):
            self.advance()
            left = ("and", left, self.parse_equality())
        return left

    def parse_equality(self):
        left = self.parse_relational()
        while self.peek() in (("op", "="), ("op", "!=")):
            left = ("cmp", self.advance()[1], left, self.parse_relational())
        return left

    def parse_relational(self):
        left = self.parse_additive()
        while self.peek() in (("op", "<"), ("op", "<="), ("op", ">"), ("op", ">=")):
            left = ("cmp", self.advance()[1], left, self.parse_additive())
        return left

    def parse_additive(self):
        left = self.parse_multiplicative()
        while self.peek() in (("op", "+"), ("op", "-")):
            left = ("arith", self.advance()[1], left, self.parse_multiplicative())
        return left

    def parse_multiplicative(self):
        left = self.parse_unary()
        # after a complete operand, `*` is multiplication, not a wildcard
        while self.peek() in (("op", "*"), ("name", "div"), ("name", "mod")):
            left = ("arith", self.advance()[1], left, self.parse_unary())
        return left

    def parse_unary(self):
        if self.peek() == ("op", "-"):
            self.advance()
            return ("neg", self.parse_unary())
        return self.parse_union()

    def parse_union(self):
        left = self.parse_path()
        while self.peek() == ("op", "|"):
            self.advance()
            left = ("union", left, self.parse_path())
        return left

    def parse_path(self):
        kind, value = self.peek()
        if kind == "op" and value == "(":
            self.advance()
            inner = self.parse_or()
            self.expect_op(")")
            return self.parse_steps_after(("expr", inner))
        if kind == "num":
            self.advance()
            return ("num", float(value))
        if kind == "lit":
            self.advance()
            return ("lit", value[1:-1])
        if (
            kind == "name"
            and self.pos + 1 < len(self.tokens)
            and self.tokens[self.pos + 1] == ("op", "(")
        ):
            self.advance()
            self.advance()
            args = []
            if self.peek() != ("op", ")"):
                args.append(self.parse_or())
                while self.peek() == ("op", ","):
                    self.advance()
                    args.append(self.parse_or())
            self.expect_op(")")
            return self.parse_steps_after(("call", value, tuple(args)))
        # a location path
        steps = []
        start = "ctx"
        if kind == "op" and value in ("/", "//"):
            start = "root"
            self.advance()
            if value == "//":
                steps.append(("descendant-or-self", None, ()))
            elif not self.at_step():
                return ("path", ("root",), ())  # bare '/'
        steps.append(self.parse_step())
        while self.peek() in (("op", "/"), ("op", "//")):
            if self.advance()[1] == "//":
                steps.append(("descendant-or-self", None, ()))
            steps.append(self.parse_step())
        return ("path", (start,), tuple(steps))

    def parse_steps_after(self, primary):
        """Predicates and an optional trailing location path after a
        primary expression, e.g. current()/../vrf."""
        predicates = []
        while self.peek() == ("op", "["):
            self.advance()
            predicates.append(self.parse_or())
            self.expect_op("]")
        steps = []
        while self.peek() in (("op", "/"), ("op", "//")):
            if self.advance()[1] == "//":
                steps.append(("descendant-or-self", None, ()))
            steps.append(self.parse_step())
        if not predicates and not steps:
            return primary[1] if primary[0] == "expr" else primary
        return ("path", ("filter", primary, tuple(predicates)), tuple(steps))

    def at_step(self):
        kind, value = self.peek()
        return kind in ("name", "dots") or (kind == "op" and value == "*")

    def parse_step(self):
        kind, value = self.advance()
        if kind == "dots":
            axis, test = ("parent" if value == ".." else "self"), None
        elif kind == "op" and value == "*":
            axis, test = "child", "*"
        elif kind == "name":
            if self.peek() == ("op", "("):
                raise _XPathUnsupported("node-type test %s()" % value)
            axis, test = "child", value
        else:
            raise _XPathUnsupported("unexpected %r in a path" % (value,))
        predicates = []
        while self.peek() == ("op", "["):
            self.advance()
            predicates.append(self.parse_or())
            self.expect_op("]")
        return (axis, test, tuple(predicates))


def _xpath_bool(value):
    if isinstance(value, list):
        return bool(value)
    if isinstance(value, float):
        return value == value and value != 0.0
    return bool(value)


def _xpath_number(value):
    if isinstance(value, list):
        value = _xpath_string(value)
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, float):
        return value
    try:
        return float(value.strip())
    except (ValueError, AttributeError):
        return float("nan")


def _xpath_string(value):
    if isinstance(value, list):
        return _xnode_string(value[0]) if value else ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        if value != value:
            return "NaN"
        try:
            if value == int(value):
                return str(int(value))
        except (OverflowError, ValueError):
            pass
        return repr(value)
    return value


def _xpath_name_match(test, meta):
    if meta is None:
        return False
    if test == "*":
        return True
    if ":" in test:
        module, _, name = test.partition(":")
        return meta.module == module and meta.yang_name == name
    return meta.yang_name == test


def _xpath_axis(nodes, axis, test):
    out = []
    if axis == "child":
        for node in nodes:
            for child in _xnode_children(node):
                if _xpath_name_match(test, child.meta):
                    out.append(child)
    elif axis == "parent":
        for node in nodes:
            if node.parent is not None and node.parent not in out:
                out.append(node.parent)
    elif axis == "self":
        out = list(nodes)
    else:  # descendant-or-self
        stack = list(nodes)
        while stack:
            node = stack.pop(0)
            if node not in out:
                out.append(node)
                stack.extend(_xnode_children(node))
    return out


def _xpath_filter(nodes, predicate, env):
    kept = []
    for index, node in enumerate(nodes):
        result = _xpath_eval(predicate, node, env)
        if isinstance(result, float):
            keep = (index + 1) == result  # positional predicate
        else:
            keep = _xpath_bool(result)
        if keep:
            kept.append(node)
    return kept


def _xpath_compare_atoms(op, a, b):
    if op in ("=", "!="):
        if isinstance(a, bool) or isinstance(b, bool):
            a, b = _xpath_bool(a), _xpath_bool(b)
        elif isinstance(a, float) or isinstance(b, float):
            a, b = _xpath_number(a), _xpath_number(b)
        return (a == b) if op == "=" else (a != b)
    a, b = _xpath_number(a), _xpath_number(b)
    if a != a or b != b:  # NaN never compares
        return False
    if op == "<":
        return a < b
    if op == "<=":
        return a <= b
    if op == ">":
        return a > b
    return a >= b


def _xpath_compare(op, left, right):
    left_ns, right_ns = isinstance(left, list), isinstance(right, list)
    if left_ns and right_ns:
        return any(
            _xpath_compare_atoms(op, _xnode_string(a), _xnode_string(b))
            for a in left
            for b in right
        )
    if left_ns or right_ns:
        nodes, other = (left, right) if left_ns else (right, left)
        flip = not left_ns  # relational ops are not symmetric
        if isinstance(other, bool):
            a, b = bool(nodes), other
            return _xpath_compare_atoms(op, b, a) if flip else _xpath_compare_atoms(op, a, b)
        for node in nodes:
            if op in ("=", "!=") and isinstance(other, str):
                equal = _xnode_equals_string(node, other)
                if (equal if op == "=" else not equal):
                    return True
            else:
                a, b = _xnode_string(node), other
                if _xpath_compare_atoms(op, b, a) if flip else _xpath_compare_atoms(op, a, b):
                    return True
        return False
    return _xpath_compare_atoms(op, left, right)


def _xsd_fullmatch(text, pattern):
    """YANG 1.1 re-match(): anchored match against an XSD regex. A
    runtime miniature of the codegen-side pattern translation: XSD has
    no anchor metacharacters, so unescaped ^/$ outside a character
    class are literal; backslash-p/-P categories and anything re cannot
    compile raise _XPathUnsupported (skipped, not misjudged)."""
    out, pos, in_class = [], 0, False
    length = len(pattern)
    while pos < length:
        char = pattern[pos]
        if char == "\\" and pos + 1 < length:
            if pattern[pos + 1] in "pP":
                raise _XPathUnsupported("re-match() with \\p/\\P categories")
            out.append(pattern[pos:pos + 2])
            pos += 2
            continue
        if not in_class and (char == "^" or char == "$"):
            out.append("\\" + char)
            pos += 1
            continue
        if char == "[":
            in_class = True
            out.append(char)
            pos += 1
            if pos < length and pattern[pos] == "^":
                out.append("^")
                pos += 1
            continue
        if char == "]":
            in_class = False
        out.append(char)
        pos += 1
    try:
        compiled = re.compile("".join(out))
    except re.error:
        raise _XPathUnsupported("re-match() pattern %r" % pattern)
    return compiled.fullmatch(text) is not None


def _xpath_call(name, args, ctx, env):
    if name == "current":
        return [env["current"]]
    values = [_xpath_eval(argument, ctx, env) for argument in args]
    if name == "not":
        return not _xpath_bool(values[0])
    if name == "true":
        return True
    if name == "false":
        return False
    if name == "boolean":
        return _xpath_bool(values[0])
    if name == "string":
        return _xpath_string(values[0]) if values else _xnode_string(ctx)
    if name == "number":
        return _xpath_number(values[0] if values else _xnode_string(ctx))
    if name == "count":
        if not isinstance(values[0], list):
            raise _XPathUnsupported("count() of a non-node-set")
        return float(len(values[0]))
    if name == "concat":
        return "".join(_xpath_string(v) for v in values)
    if name == "contains":
        return _xpath_string(values[1]) in _xpath_string(values[0])
    if name == "starts-with":
        return _xpath_string(values[0]).startswith(_xpath_string(values[1]))
    if name == "re-match":
        return _xsd_fullmatch(_xpath_string(values[0]), _xpath_string(values[1]))
    if name == "string-length":
        return float(len(_xpath_string(values[0]) if values else _xnode_string(ctx)))
    if name in ("derived-from", "derived-from-or-self"):
        nodes = values[0]
        if not isinstance(nodes, list):
            raise _XPathUnsupported("%s() of a non-node-set" % name)
        target = _xpath_string(values[1])

        def target_matches(canonical):
            # prefixes were normalized to module names at codegen; a
            # bare target matches on the local name
            if ":" in target:
                return canonical == target
            return canonical.split(":", 1)[-1] == target

        for node in nodes:
            value = _xnode_string(node)
            id_map = node.meta.identity_map if node.meta is not None else None
            canonical = id_map.get(value) if id_map else None
            if canonical is None:
                # value outside the known hierarchy: keep the exact
                # or-self shortcut, otherwise skip rather than misjudge
                if name == "derived-from-or-self" and value.split(":")[-1] == target.split(":")[-1]:
                    return True
                raise _XPathUnsupported(
                    "%s() on a value outside the identity table" % name
                )
            if name == "derived-from-or-self" and target_matches(canonical):
                return True
            seen = set()
            stack = list(_YANG_IDENTITY_BASES.get(canonical, ()))
            while stack:
                base = stack.pop()
                if base in seen:
                    continue
                seen.add(base)
                if target_matches(base):
                    return True
                stack.extend(_YANG_IDENTITY_BASES.get(base, ()))
        return False
    raise _XPathUnsupported("function %s()" % name)


def _xpath_eval(ast, ctx, env):
    op = ast[0]
    if op in ("num", "lit"):
        return ast[1]
    if op == "or":
        return _xpath_bool(_xpath_eval(ast[1], ctx, env)) or _xpath_bool(
            _xpath_eval(ast[2], ctx, env)
        )
    if op == "and":
        return _xpath_bool(_xpath_eval(ast[1], ctx, env)) and _xpath_bool(
            _xpath_eval(ast[2], ctx, env)
        )
    if op == "cmp":
        return _xpath_compare(
            ast[1], _xpath_eval(ast[2], ctx, env), _xpath_eval(ast[3], ctx, env)
        )
    if op == "arith":
        left = _xpath_number(_xpath_eval(ast[2], ctx, env))
        right = _xpath_number(_xpath_eval(ast[3], ctx, env))
        symbol = ast[1]
        try:
            if symbol == "+":
                return left + right
            if symbol == "-":
                return left - right
            if symbol == "*":
                return left * right
            if symbol == "div":
                return left / right
            return left - right * float(int(left / right))  # mod, sign of left
        except (ZeroDivisionError, OverflowError, ValueError):
            return float("nan")
    if op == "neg":
        return -_xpath_number(_xpath_eval(ast[1], ctx, env))
    if op == "union":
        left = _xpath_eval(ast[1], ctx, env)
        right = _xpath_eval(ast[2], ctx, env)
        if not isinstance(left, list) or not isinstance(right, list):
            raise _XPathUnsupported("'|' of non-node-sets")
        return left + [node for node in right if node not in left]
    if op == "call":
        return _xpath_call(ast[1], ast[2], ctx, env)
    # op == "path"
    start = ast[1]
    if start[0] == "root":
        nodes = [env["doc"]]
    elif start[0] == "ctx":
        nodes = [ctx]
    else:  # ("filter", primary, predicates)
        value = _xpath_eval(start[1], ctx, env)
        if not isinstance(value, list):
            raise _XPathUnsupported("path step on a non-node-set")
        nodes = value
        for predicate in start[2]:
            nodes = _xpath_filter(nodes, predicate, env)
    for axis, test, predicates in ast[2]:
        nodes = _xpath_axis(nodes, axis, test)
        for predicate in predicates:
            nodes = _xpath_filter(nodes, predicate, env)
    return nodes


_xpath_ast_cache = {}


def _xpath_check(expression, ctx, doc):
    """Boolean outcome of a must/when expression with context node `ctx`
    (which is also what current() returns)."""
    ast = _xpath_ast_cache.get(expression)
    if ast is None:
        ast = _XPathParser(_xpath_tokenize(expression)).parse()
        _xpath_ast_cache[expression] = ast
    return _xpath_bool(_xpath_eval(ast, ctx, {"current": ctx, "doc": doc}))
'''

# Runtime embedded at the top of the generated module unless
# --no-dataclass-validation is given. Stdlib-only, so the generated file
# stays dependency-free. _YangNode.__setattr__ is hidden from type checkers: a
# visible `__setattr__(self, name: str, value: object)` would make mypy and
# pyright accept assignment of anything to any attribute, destroying
# exactly the static guarantees this backend exists to provide.
_VALIDATION_RUNTIME = '''
class YangValidationError(ValueError):
    """A value assigned to a generated field violates its YANG type."""


_II_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]*(?::[A-Za-z_][A-Za-z0-9_.-]*)?")


def _instance_identifier_syntax_ok(text):
    """Purely syntactic RFC 7950 9.13 instance-identifier shape check:
    an absolute path of /name steps with balanced, quote-respecting
    [...] predicates. Predicate contents and schema validity are not
    judged (require-instance semantics need a schema-aware resolver),
    so this accepts a superset of what libyang accepts -- but rejects
    values that are not paths at all."""
    if not text.startswith("/"):
        return False
    pos, length = 0, len(text)
    while pos < length:
        if text[pos] != "/":
            return False
        pos += 1
        match = _II_NAME_RE.match(text, pos)
        if match is None:
            return False
        pos = match.end()
        while pos < length and text[pos] == "[":
            pos += 1
            quote = None
            while pos < length:
                char = text[pos]
                if quote is not None:
                    if char == quote:
                        quote = None
                elif char == "'" or char == '"':
                    quote = char
                elif char == "]":
                    break
                pos += 1
            if pos >= length or text[pos] != "]":
                return False
            pos += 1
    return True


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
    inverted_patterns: tuple = ()  # `modifier invert-match`: must NOT fullmatch
    values: tuple = ()  # allowed enum/identityref strings; () = unrestricted
    # identityrefs are CLOSED sets: an empty derived set in the compiled
    # modules means no value is valid (not "unrestricted")
    closed: bool = False
    bits: tuple = ()  # allowed bit names; () = unrestricted
    members: tuple = ()  # union member checks; at least one must pass
    fraction_digits: int = 0  # decimal64 precision; 0 = not a decimal64
    natives: tuple = ()  # allowed classes when base == "native"

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
        if self.fraction_digits:
            self._check_decimal64(value, path)
        if self.values and value not in self.values:
            raise YangValidationError(
                "%s: %r is not one of the allowed values %s" % (path, value, list(self.values))
            )
        if self.closed and not self.values:
            raise YangValidationError(
                "%s: %r cannot be valid -- no identity derived from the "
                "base is in the compiled module set" % (path, value)
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
        for pattern in self.inverted_patterns:
            if re.fullmatch(pattern, value) is not None:
                raise YangValidationError(
                    "%s: %r matches invert-match pattern %r" % (path, value, pattern)
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
        elif base == "instance-identifier":
            ok = isinstance(value, str)
            if ok and not _instance_identifier_syntax_ok(value):
                raise YangValidationError(
                    "%s: %r is not instance-identifier syntax "
                    "(absolute /module:node[...] path)" % (path, value)
                )
        elif base == "ipv4-address":
            ok = isinstance(value, ipaddress.IPv4Address)
        elif base == "ipv6-address":
            ok = isinstance(value, ipaddress.IPv6Address)
        elif base == "ipv6-address-no-zone":
            ok = isinstance(value, ipaddress.IPv6Address) and value.scope_id is None
        elif base == "ipv4-prefix":
            ok = isinstance(value, ipaddress.IPv4Network)
        elif base == "ipv6-prefix":
            ok = isinstance(value, ipaddress.IPv6Network)
        elif base == "native":
            if not isinstance(value, self.natives):
                raise YangValidationError(
                    "%s: expected an instance of %s, got %r"
                    % (path, " | ".join(c.__name__ for c in self.natives), value)
                )
            ok = True
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

    def _check_decimal64(self, value, path):
        dec = value if isinstance(value, decimal.Decimal) else decimal.Decimal(str(value))
        exponent = dec.normalize().as_tuple().exponent
        if isinstance(exponent, int) and -exponent > self.fraction_digits:
            raise YangValidationError(
                "%s: %s has more than fraction-digits %d decimal places"
                % (path, dec, self.fraction_digits)
            )
        # implied decimal64 range: -2**63 .. 2**63-1, scaled by fraction-digits
        low = decimal.Decimal(-(2**63)).scaleb(-self.fraction_digits)
        high = decimal.Decimal(2**63 - 1).scaleb(-self.fraction_digits)
        if not low <= dec <= high:
            raise YangValidationError(
                "%s: %s is outside the decimal64 range %s..%s" % (path, dec, low, high)
            )


class _YangNode:
    """Base of every generated dataclass: validates each assignment
    (dataclass __init__ assigns through __setattr__ too)."""

    _yang_fields: typing.ClassVar[dict] = {}

    if not typing.TYPE_CHECKING:

        def __setattr__(self, name, value):
            meta = self._yang_fields.get(name)
            if meta is not None and meta.check is not None and value is not None:
                path = "%s.%s" % (type(self).__name__, name)
                if isinstance(value, list):
                    for index, element in enumerate(value):
                        meta.check.validate(element, "%s[%d]" % (path, index))
                else:
                    meta.check.validate(value, path)
            object.__setattr__(self, name, value)


def validate_tree(*roots):
    """Whole-tree validation of one or more binding roots.

    Checks everything on-assignment validation cannot: re-validates every
    leaf and leaf-list element (covering in-place list mutation such as
    ``.append()``), mandatory leaves, list keys (present and unique),
    ``unique`` groups, leaf-list value uniqueness, min-/max-elements,
    choice exclusivity and mandatory choices, leafref referential
    integrity across all the given roots, and YANG ``must``/``when``
    expressions (evaluated with an embedded XPath 1.0 subset engine
    wherever the constrained node exists; absolute paths resolve across
    all the given roots, so pass every module root the expressions
    reach into). Existence follows YANG (RFC 7950): a non-presence
    container exists implicitly wherever its surrounding context
    exists, so mandatory nodes inside it (mandatory leaves and
    choices, min-elements) are enforced even when the container holds
    no other data -- mandatory-ness propagates up through chains of
    empty non-presence containers, and is stopped only by a presence
    container (absent unless some descendant is set: present-but-empty
    is not representable by these bindings), by an unselected case, or
    by a `when`-guarded container (whose checks are skipped when it is
    empty rather than misjudged). List entries and the passed roots
    always count as existing context. Collects every violation and
    raises a single YangValidationError listing them all."""
    errors = []
    leaf_values = {}  # target schema path -> values present in the trees
    leafref_uses = []  # (instance path, target schema path, value)
    constraints = []  # (is_must, context _XNode, expression, message, path)
    # when-guarded empty non-presence containers whose pending checks
    # apply only if their when conditions hold: resolved after the walk
    guarded_deferred = []
    doc = _XNode(None, None, None, roots=list(roots))

    def queue_constraints(meta, xnode, fpath):
        for expression, message in meta.musts:
            constraints.append((True, xnode, expression, message, fpath))
        for expression, self_context in meta.whens:
            context = xnode if self_context else xnode.parent
            constraints.append((False, context, expression, None, fpath))

    def note_value(schema_path, value):
        try:
            leaf_values.setdefault(schema_path, set()).add(value)
        except TypeError:
            pass  # unhashable (e.g. a bits instance inside a union)

    def check_value(check, value, path):
        if check is None or value is None:
            return
        try:
            check.validate(value, path)
        except YangValidationError as exc:
            errors.append(str(exc))

    def walk(node, path, schema_path, parent_module, xself, force_exists=False):
        fields = getattr(type(node), "_yang_fields", {})
        active_cases = {}  # choice -> set of cases with data
        # Pending checks: (case-or-None, kind, payload). kind "error"
        # carries a message; kind "must" carries queue_constraints args
        # for a must on an empty non-presence container (RFC 7950: such
        # a container exists implicitly, so libyang evaluates its musts
        # too). A pending check fires once this node exists -- directly,
        # by force (list entries, roots), or implicitly as an empty
        # non-presence container hoisted into an existing parent context,
        # in which case only case-free items travel (an implicitly
        # existing node selects no case).
        pending = []
        exists = False
        for fname, meta in fields.items():
            value = getattr(node, fname, None)
            qualified = _qualified_name(meta, parent_module)
            fpath = "%s/%s" % (path, qualified)
            fschema = "%s/%s" % (schema_path, qualified)
            field_set = False
            if meta.kind == "container":
                if value is not None:
                    xchild = _XNode(value, meta, xself)
                    field_set, hoisted = walk(value, fpath, fschema, meta.module, xchild)
                    if field_set:
                        queue_constraints(meta, xchild, fpath)
                    elif not meta.presence:
                        # Empty non-presence container: it exists
                        # implicitly wherever this node exists, so its
                        # hoisted mandatory checks and its own musts
                        # stay pending here, gated on this container's
                        # case (if any). An empty presence container is
                        # absent and contributes nothing. A when-guarded
                        # container's checks are deferred until after
                        # the walk, when its when conditions can be
                        # evaluated against the finished tree (they
                        # apply only if every when is true; conditions
                        # outside the XPath subset skip the checks,
                        # never misjudging).
                        if meta.whens:
                            pending.append(
                                (
                                    meta.case,
                                    "guarded",
                                    (meta, xchild, fpath, hoisted),
                                )
                            )
                        else:
                            for _case, kind, payload in hoisted:
                                pending.append((meta.case, kind, payload))
                            if meta.musts:
                                pending.append(
                                    (meta.case, "must", (meta, xchild, fpath))
                                )
            elif meta.kind == "list":
                entries = value or []
                seen_keys = set()
                seen_unique = {group: set() for group in meta.unique}
                for index, entry in enumerate(entries):
                    key_text = _instance_key(entry, meta)
                    epath = "%s[%s]" % (fpath, index if key_text is None else key_text)
                    xentry = _XNode(entry, meta, xself)
                    walk(entry, epath, fschema, meta.module, xentry, force_exists=True)
                    queue_constraints(meta, xentry, epath)
                    field_set = True
                    for key in meta.keys:
                        if getattr(entry, key, None) is None:
                            errors.append("%s: list key '%s' is not set" % (epath, key))
                    if meta.keys:
                        key_tuple = tuple(getattr(entry, k, None) for k in meta.keys)
                        if None not in key_tuple:
                            if key_tuple in seen_keys:
                                errors.append(
                                    "%s: duplicate list key %r" % (epath, key_tuple)
                                )
                            seen_keys.add(key_tuple)
                    for group in meta.unique:
                        values = []
                        for leaf_path in group:
                            obj = entry
                            for step in leaf_path[:-1]:
                                obj = getattr(obj, step, None)
                                if obj is None:
                                    break
                            values.append(
                                getattr(obj, leaf_path[-1], None)
                                if obj is not None
                                else None
                            )
                        values = tuple(values)
                        if None in values:
                            continue  # unique applies only where all leaves exist
                        if values in seen_unique[group]:
                            errors.append(
                                "%s: violates unique %r: %r is already present"
                                % (
                                    epath,
                                    " ".join("/".join(p) for p in group),
                                    values,
                                )
                            )
                        seen_unique[group].add(values)
                if meta.max_elements is not None and len(entries) > meta.max_elements:
                    errors.append(
                        "%s: %d entries exceed max-elements %d"
                        % (fpath, len(entries), meta.max_elements)
                    )
                if meta.min_elements and len(entries) < meta.min_elements:
                    pending.append(
                        (
                            meta.case,
                            "error",
                            "%s: %d entries, fewer than min-elements %d"
                            % (fpath, len(entries), meta.min_elements),
                        )
                    )
            elif meta.kind == "leaf-list":
                elements = value or []
                seen_elements = set()
                for index, element in enumerate(elements):
                    epath = "%s[%d]" % (fpath, index)
                    check_value(meta.check, element, epath)
                    note_value(fschema, element)
                    if meta.leafref is not None:
                        leafref_uses.append((epath, meta.leafref, element))
                    if meta.musts or meta.whens:
                        queue_constraints(
                            meta,
                            _XNode(None, meta, xself, value=element, is_leaf=True),
                            epath,
                        )
                    try:
                        if element in seen_elements:
                            errors.append(
                                "%s: duplicate leaf-list value %r" % (epath, element)
                            )
                        seen_elements.add(element)
                    except TypeError:
                        pass
                if meta.max_elements is not None and len(elements) > meta.max_elements:
                    errors.append(
                        "%s: %d elements exceed max-elements %d"
                        % (fpath, len(elements), meta.max_elements)
                    )
                if meta.min_elements and len(elements) < meta.min_elements:
                    pending.append(
                        (
                            meta.case,
                            "error",
                            "%s: %d elements, fewer than min-elements %d"
                            % (fpath, len(elements), meta.min_elements),
                        )
                    )
                field_set = bool(elements)
            else:  # leaf (or one bool bit of a bits dataclass)
                if meta.cls is not None and isinstance(value, meta.cls):
                    # a bits dataclass: re-check its bit fields; it counts
                    # as set only when some bit is set (truthy)
                    walk(value, fpath, fschema, meta.module, _XNode(value, meta, xself))
                    field_set = bool(value)
                elif value is not None:
                    check_value(meta.check, value, fpath)
                    if meta.kind == "leaf":
                        note_value(fschema, value)
                        if meta.leafref is not None:
                            leafref_uses.append((fpath, meta.leafref, value))
                    field_set = True
                if field_set and (meta.musts or meta.whens):
                    queue_constraints(
                        meta,
                        _XNode(None, meta, xself, value=value, is_leaf=True),
                        fpath,
                    )
                if meta.kind == "leaf" and meta.mandatory and value is None:
                    pending.append(
                        (meta.case, "error", "%s: mandatory leaf is not set" % fpath)
                    )
            if field_set:
                exists = True
                if meta.case is not None:
                    # setting a field selects every case on its chain,
                    # innermost and enclosing ones alike
                    for choice, case_name in meta.case:
                        active_cases.setdefault(choice, set()).add(case_name)
        for choice, cases in active_cases.items():
            if len(cases) > 1:
                errors.append(
                    "%s: fields of multiple cases of choice '%s' are set: %s"
                    % (path or "/", choice, ", ".join(sorted(cases)))
                )
        for choice, (mandatory, chain) in getattr(
            type(node), "_yang_choices", {}
        ).items():
            if mandatory and not active_cases.get(choice):
                # a nested mandatory choice applies only while every
                # enclosing case on its chain is the selected one
                pending.append(
                    (
                        chain or None,
                        "error",
                        "%s: no case of mandatory choice '%s' is set"
                        % (path or "/", choice),
                    )
                )
        hoisted = []
        if exists or force_exists:
            for case, kind, payload in pending:
                if case is not None and any(
                    case_name not in active_cases.get(choice, ())
                    for choice, case_name in case
                ):
                    continue
                if kind == "error":
                    errors.append(payload)
                elif kind == "guarded":
                    guarded_deferred.append(payload)
                else:
                    queue_constraints(*payload)
        else:
            # This node holds no data; whether its checks apply is the
            # parent's call (they do if this is a non-presence container
            # in an existing, when-free context). Case-tagged items stay
            # behind: an implicitly existing node selects no case.
            hoisted = [item for item in pending if item[0] is None]
        return (exists, hoisted)

    for root in roots:
        # A passed root is the datastore itself: it exists, so even
        # top-level mandatory nodes (legal, if unusual, YANG) apply.
        walk(root, "", "", None, doc, force_exists=True)

    def resolve_guarded(payload):
        meta, xchild, fpath, items = payload
        for expression, self_context in meta.whens:
            context = xchild if self_context else xchild.parent
            try:
                if not _xpath_check(expression, context, doc):
                    return
            except _XPathUnsupported:
                return  # outside the subset: skip, never misjudge
        for _case, kind, inner in items:
            if kind == "error":
                errors.append(inner)
            elif kind == "guarded":
                resolve_guarded(inner)
            else:
                queue_constraints(*inner)
        if meta.musts:
            queue_constraints(meta, xchild, fpath)

    for payload in guarded_deferred:
        resolve_guarded(payload)
    for path, target, value in leafref_uses:
        if value not in leaf_values.get(target, ()):
            errors.append(
                "%s: leafref %r has no matching instance at %s" % (path, value, target)
            )
    for is_must, context, expression, message, fpath in constraints:
        if context is None:
            continue
        try:
            satisfied = _xpath_check(expression, context, doc)
        except _XPathUnsupported:
            continue  # outside the evaluated subset: skip, never misjudge
        if satisfied:
            continue
        if is_must:
            errors.append(
                "%s: %s" % (fpath, message or "violates must %r" % expression)
            )
        else:
            errors.append(
                "%s: node is present but its when condition %r is false"
                % (fpath, expression)
            )
    if errors:
        raise YangValidationError(
            "%d violation(s):\\n  %s" % (len(errors), "\\n  ".join(errors))
        )
'''


# Serialisation runtime, embedded with --dataclass-serde. Driven entirely
# by the _FieldMeta tables; stdlib-only. Errors raise ValueError (of which
# YangValidationError, when generated, is a subclass) so callers can catch
# uniformly whether or not validation is generated.
_SERDE_RUNTIME = '''
_NATIVE_IP_TYPES = (
    ipaddress.IPv4Address,
    ipaddress.IPv6Address,
    ipaddress.IPv4Network,
    ipaddress.IPv6Network,
)


def _encode_value(meta, value):
    if meta.cls is not None and isinstance(value, meta.cls):
        # a bits dataclass -> RFC 7951 space-separated set-bit names
        table = getattr(meta.cls, "_yang_fields", {})
        return " ".join(m.yang_name for f, m in table.items() if getattr(value, f))
    if meta.encode == "int64":
        return str(value)  # RFC 7951: 64-bit ints are JSON strings
    if meta.encode == "decimal":
        return str(value)
    if meta.encode == "empty":
        return [None]
    if meta.encode == "binary" and isinstance(value, (bytes, bytearray)):
        return base64.b64encode(bytes(value)).decode("ascii")
    if meta.encode == "identityref" and meta.identity_map is not None:
        return meta.identity_map.get(value, value)
    if meta.encode == "bits" and isinstance(value, (set, frozenset)):
        return " ".join(sorted(value))
    if meta.encode in ("ip-address", "ip-prefix", "native"):
        return str(value)
    if meta.natives and isinstance(value, meta.natives):
        return str(value)  # a native-hinted member of a union
    if isinstance(value, (set, frozenset)):
        return " ".join(sorted(value))  # a bits member of a union
    if isinstance(value, decimal.Decimal):
        return str(value)
    if isinstance(value, _NATIVE_IP_TYPES):
        return str(value)  # a native member of a union carries no encode tag
    return value


def _encode_annotation_value(adef, value):
    """RFC 7952 5.2.1: an annotation value is encoded exactly like a
    leaf of the same type."""
    if adef.encode == "int64":
        return str(value)
    if adef.encode == "decimal":
        return str(value)
    if adef.encode == "binary" and isinstance(value, (bytes, bytearray)):
        return base64.b64encode(bytes(value)).decode("ascii")
    if adef.encode in ("ip-address", "ip-prefix"):
        return str(value)
    if isinstance(value, decimal.Decimal):
        return str(value)
    return value


def _encode_metadata(entry):
    """One RFC 7952 metadata object: annotation values keyed by the
    always-qualified `module-name:annotation` form (RFC 7952 5.2.1)."""
    out = {}
    for name, value in entry.items():
        adef = _YANG_ANNOTATIONS.get(name)
        if adef is None:
            raise ValueError("unknown annotation %r attached to a node" % (name,))
        out["%s:%s" % (adef.module, adef.yang_name)] = _encode_annotation_value(
            adef, value
        )
    return out


def to_ietf_json(root):
    """RFC 7951 (JSON encoding of YANG data) representation of a bindings
    tree, as a plain dict ready for json.dumps(). Unset leaves and empty
    non-presence containers are omitted; member names are qualified with
    the module name at the top level and wherever the module changes.
    RFC 7952 annotations (see annotate()) are encoded per its section
    5.2: a "@" metadata-object member inside container / list-entry
    objects, a sibling "@member" object for annotated leaf members, and
    a null-padded "@member" array for annotated leaf-list entries --
    but only where the annotated instance is itself encoded (metadata
    on unset leaves, or on leaf-list indexes past the end of the list,
    is dropped). Limitations: a present-but-empty presence container is
    omitted (the dataclass shape cannot express container absence)
    unless annotated, and union members are encoded by their Python
    value type."""

    def encode_node(node, parent_module):
        out = {}
        fields = getattr(type(node), "_yang_fields", {})
        for fname, meta in fields.items():
            value = getattr(node, fname, None)
            key = _qualified_name(meta, parent_module)
            if meta.kind == "container":
                if value is not None:
                    child = encode_node(value, meta.module)
                    if child:
                        out[key] = child
            elif meta.kind == "list":
                entries = [encode_node(entry, meta.module) for entry in value or []]
                if entries:
                    out[key] = entries
            elif meta.kind == "leaf-list":
                if value:
                    out[key] = [_encode_value(meta, element) for element in value]
            elif meta.kind == "leaf":
                if meta.cls is not None and isinstance(value, meta.cls):
                    if value:  # bits: present iff any bit is set
                        out[key] = _encode_value(meta, value)
                elif value is not None:
                    out[key] = _encode_value(meta, value)
        store = getattr(node, "_yang_metadata", None) or {}
        if store.get(None):
            out["@"] = _encode_metadata(store[None])
        for skey, entry in store.items():
            if skey is None or not entry:
                continue
            fname = skey[0] if isinstance(skey, tuple) else skey
            meta = fields.get(fname)
            if meta is None:
                continue
            key = _qualified_name(meta, parent_module)
            if key not in out:
                continue  # metadata on an unset member is dropped
            if meta.kind == "leaf":
                out["@" + key] = _encode_metadata(entry)
            elif meta.kind == "leaf-list":
                index = skey[1]
                if index >= len(out[key]):
                    continue  # dangling entry index
                array = out.setdefault("@" + key, [])
                if len(array) <= index:
                    array.extend([None] * (index + 1 - len(array)))
                array[index] = _encode_metadata(entry)
        return out

    return encode_node(root, None)


def _union_flat(check):
    """Union member checks with nested unions flattened."""
    for member in check.members:
        if getattr(member, "base", None) == "union":
            yield from _union_flat(member)
        else:
            yield member


def _int64_member(member):
    """Whether an int member check is 64-bit (string-encoded, RFC 7951)."""
    bounds = [b for stmt in member.ranges for pair in stmt for b in pair]
    return any(b is not None and abs(b) > 2**32 for b in bounds)


def _decode_union_member(members, value):
    """First union member (in YANG order) that accepts `value` decoded
    into that member's representation, or None when no member does
    (the caller then leaves the raw value for on-assignment checking
    to judge). Only conversions whose RFC 7951 JSON encoding is a
    string are attempted; a str-typed member keeps the string."""
    for member in members:
        base = getattr(member, "base", None)
        if base == "union":
            candidate = _decode_union_member(member.members, value)
            if candidate is not None:
                return candidate
            continue
        if base == "str":
            candidate = value
        elif base == "decimal":
            try:
                candidate = decimal.Decimal(value)
            except decimal.InvalidOperation:
                continue
        elif base == "bits":
            candidate = frozenset(value.split())
        elif base == "bytes":
            try:
                candidate = base64.b64decode(value, validate=True)
            except (ValueError, TypeError):
                continue
        elif base == "int":
            # only 64-bit integers are string-encoded (RFC 7951 6.1);
            # narrower ints encode as JSON numbers, so a string must
            # not match them
            if not _int64_member(member):
                continue
            try:
                candidate = int(value)
            except ValueError:
                continue
        else:
            continue
        try:
            member.validate(candidate, "")
            return candidate
        except ValueError:
            continue
    return None


def _decode_value(meta, value):
    if meta.cls is not None and isinstance(value, str):
        # a bits leaf: space-separated bit names -> bits dataclass
        names = set(value.split())
        table = getattr(meta.cls, "_yang_fields", {})
        known = {m.yang_name for m in table.values()}
        unknown = names - known
        if unknown:
            raise ValueError(
                "%r: unknown bit name(s) %s" % (value, sorted(unknown))
            )
        return meta.cls(**{f: True for f, m in table.items() if m.yang_name in names})
    if meta.encode == "int64":
        # RFC 7951 6.1: 64-bit integers are encoded as JSON strings
        if not isinstance(value, str):
            raise ValueError(
                "%r: a 64-bit integer must be a JSON string (RFC 7951)" % (value,)
            )
        return int(value)
    if meta.encode == "decimal":
        # RFC 7951 6.1: decimal64 is encoded as a JSON string
        if not isinstance(value, str):
            raise ValueError(
                "%r: a decimal64 value must be a JSON string (RFC 7951)"
                % (value,)
            )
        return decimal.Decimal(value)
    if meta.encode == "empty":
        # RFC 7951 6.9: type empty is encoded as [null]
        if value != [None]:
            raise ValueError(
                "%r: an empty leaf must be encoded as [null] (RFC 7951)" % (value,)
            )
        return True  # [null] -> presence
    if meta.encode == "binary" and isinstance(value, str):
        return base64.b64decode(value)
    if meta.encode == "ip-address" and isinstance(value, str):
        return ipaddress.ip_address(value)
    if meta.encode == "ip-prefix" and isinstance(value, str):
        return ipaddress.ip_network(value, strict=False)
    if meta.encode == "native" and isinstance(value, str):
        for cls in meta.natives:
            try:
                return cls(value)
            except (ValueError, TypeError):
                continue
        return value
    if meta.encode == "ip-union" and isinstance(value, str):
        # a union mixing native (ipaddress / native-type-hinted) members
        # with other types: try the native parses, keep the first
        # candidate the union check accepts, and otherwise leave the
        # string for the other members. Bare-address parse goes first so
        # an address-only string never gains a /32; hint constructors
        # before ip_network so ADDR/PREFIXLEN keeps its host bits.
        for parse in (
            ipaddress.ip_address,
            *meta.natives,
            lambda v: ipaddress.ip_network(v, strict=False),
        ):
            try:
                candidate = parse(value)
            except (ValueError, TypeError):
                continue
            if meta.check is None:
                return candidate
            try:
                meta.check.validate(candidate, "")
                return candidate
            except ValueError:
                continue
        return value
    if meta.check is not None and getattr(meta.check, "base", None) == "union":
        if isinstance(value, str):
            # A general union: RFC 7951 encodes some member types as JSON
            # strings (64-bit ints, decimal64, bits, binary), so try the
            # members in YANG order, converting the string into each
            # member's representation, and keep the first that validates.
            candidate = _decode_union_member(meta.check.members, value)
            if candidate is not None:
                return candidate
        elif isinstance(value, bool):
            if not any(m.base == "bool" for m in _union_flat(meta.check)):
                raise ValueError(
                    "%r: no boolean member in the union (RFC 7951)" % (value,)
                )
        elif isinstance(value, int):
            # a JSON number matches only int members narrower than 64
            # bits (RFC 7951 6.1: 64-bit ints are strings)
            ok = False
            for m in _union_flat(meta.check):
                if m.base != "int" or _int64_member(m):
                    continue
                try:
                    m.validate(value, "")
                    ok = True
                    break
                except ValueError:
                    continue
            if not ok:
                raise ValueError(
                    "%r: no union member takes this JSON number "
                    "(RFC 7951: 64-bit ints and decimal64 are strings)"
                    % (value,)
                )
        elif isinstance(value, float):
            raise ValueError(
                "%r: no YANG type is encoded as a JSON fraction "
                "(decimal64 is a string, RFC 7951 6.1)" % (value,)
            )
    if meta.encode == "identityref" and isinstance(value, str) and meta.identity_map:
        # RFC 7951 6.8: the qualifier of an identityref value is the
        # DEFINING MODULE's name, and may be omitted only when that is
        # the leaf's own module. YANG-prefix spellings and cross-module
        # bare names are assignment-time conveniences, not valid JSON.
        canonical = meta.identity_map.get(value)
        if canonical is not None:
            if ":" in value:
                if value != canonical:
                    raise ValueError(
                        "%r: an identityref is module-qualified as %r in "
                        "RFC 7951 JSON (YANG-prefix spellings are not "
                        "valid)" % (value, canonical)
                    )
            elif canonical.split(":", 1)[0] != meta.module:
                raise ValueError(
                    "%r: identity defined outside the leaf's module must "
                    "be module-qualified as %r (RFC 7951 6.8)"
                    % (value, canonical)
                )
        # Normalise the accepted spelling to the preferred one -- bare
        # unless the bare name is claimed by a different identity -- so a
        # decoded tree compares equal to one built with defaults/bare
        # assignments.
        if canonical is not None:
            accepted = [k for k, v in meta.identity_map.items() if v == canonical]
            if accepted:
                return min(accepted, key=lambda s: (":" in s, s))
    return value


def from_ietf_json(cls, data):
    """Build a bindings tree of type `cls` from an RFC 7951 dict (the
    shape json.load() gives). Member names are accepted both module-
    qualified and bare; unknown members raise ValueError. RFC 7952
    metadata members ("@" / "@member", section 5.2) are decoded into
    annotations (see annotate()); unknown annotation names raise
    ValueError. Values flow through normal attribute assignment, so
    on-assignment validation (when generated) applies; run
    validate_tree() afterwards for structural/referential checks."""
    node = cls()
    _decode_into(node, data)
    return node


def _decode_annotation_value(adef, value):
    if adef.encode == "int64" and isinstance(value, str):
        return int(value)
    if adef.encode == "decimal":
        return decimal.Decimal(str(value))
    if adef.encode == "binary" and isinstance(value, str):
        return base64.b64decode(value)
    if adef.encode == "ip-address" and isinstance(value, str):
        return ipaddress.ip_address(value)
    if adef.encode == "ip-prefix" and isinstance(value, str):
        return ipaddress.ip_network(value, strict=False)
    return value


def _apply_metadata(node, member, index, obj):
    """Decode one RFC 7952 metadata object onto a node / leaf member /
    leaf-list entry. Annotation names must be the qualified
    `module-name:annotation` form (RFC 7952 5.2.1); the bare name is
    accepted leniently when unambiguous."""
    values = {}
    for key, value in obj.items():
        for pyname, adef in _YANG_ANNOTATIONS.items():
            if key in ("%s:%s" % (adef.module, adef.yang_name), adef.yang_name):
                values[pyname] = _decode_annotation_value(adef, value)
                break
        else:
            raise ValueError("unknown metadata annotation %r" % (key,))
    annotate(node, member, index, **values)


def _decode_into(node, data):
    fields = getattr(type(node), "_yang_fields", {})
    by_name = {}
    for fname, meta in fields.items():
        by_name["%s:%s" % (meta.module, meta.yang_name)] = (fname, meta)
        by_name.setdefault(meta.yang_name, (fname, meta))
    metadata = []
    for key, value in data.items():
        if key.startswith("@"):
            # RFC 7952 metadata member; deferred until the data members
            # (in particular annotated leaf-lists) have been decoded.
            metadata.append((key, value))
            continue
        try:
            fname, meta = by_name[key]
        except KeyError:
            raise ValueError(
                "%s has no member %r" % (type(node).__name__, key)
            ) from None
        if meta.kind == "container":
            if meta.presence and not value:
                # The bindings auto-instantiate containers and infer
                # presence from held data, so a present-but-empty
                # presence container has no representation -- decoding
                # it silently as absent would change its meaning.
                raise ValueError(
                    "%r: a present-but-empty presence container is not "
                    "representable by these bindings" % (key,)
                )
            _decode_into(getattr(node, fname), value)
        elif meta.kind == "list":
            entries = []
            for item in value:
                entry = meta.cls()
                _decode_into(entry, item)
                entries.append(entry)
            setattr(node, fname, entries)
        elif meta.kind == "leaf-list":
            setattr(node, fname, [_decode_value(meta, element) for element in value])
        else:
            setattr(node, fname, _decode_value(meta, value))
    for key, value in metadata:
        if key == "@":
            _apply_metadata(node, None, None, value)
            continue
        try:
            fname, meta = by_name[key[1:]]
        except KeyError:
            raise ValueError(
                "%s has no member %r to annotate" % (type(node).__name__, key[1:])
            ) from None
        if meta.kind == "leaf":
            _apply_metadata(node, fname, None, value)
        elif meta.kind == "leaf-list":
            for index, obj in enumerate(value):
                if obj:  # null = no annotations on this entry
                    _apply_metadata(node, fname, index, obj)
        else:
            raise ValueError(
                "%s: %r -- container/list-entry metadata goes inside the "
                "object as '@' (RFC 7952 5.2.2), not on the parent"
                % (type(node).__name__, key)
            )
    return node
'''


# XPath runtime, embedded with --dataclass-xpaths. The schema paths live
# in per-class _yang_schema_path ClassVars; this adds instance paths.
_XPATH_RUNTIME = '''
def data_path(root, node):
    """Instance path of `node` (a container / list-entry / bits dataclass
    of the tree under `root`), with [key=value] predicates on list steps;
    None when `node` is not in the tree. Dataclasses carry no parent
    pointers, so the path is computed by a root-relative search."""
    if node is root:
        return "/"

    def search(current, path, parent_module):
        for fname, meta in getattr(type(current), "_yang_fields", {}).items():
            value = getattr(current, fname, None)
            if value is None:
                continue
            step = "%s/%s" % (path, _qualified_name(meta, parent_module))
            if meta.kind == "container":
                if value is node:
                    return step
                found = search(value, step, meta.module)
                if found is not None:
                    return found
            elif meta.kind == "list":
                for index, entry in enumerate(value):
                    key_text = _instance_key(entry, meta)
                    epath = "%s[%s]" % (step, index if key_text is None else key_text)
                    if entry is node:
                        return epath
                    found = search(entry, epath, meta.module)
                    if found is not None:
                        return found
            elif meta.cls is not None and value is node:  # a bits instance
                return step
        return None

    return search(root, "", None)
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
            "--dataclass-serde",
            dest="dataclass_serde",
            action="store_true",
            default=False,
            help="Generate to_ietf_json()/from_ietf_json() functions "
            "(RFC 7951 JSON encoding of YANG data, as plain dicts) into "
            "the module",
        )
        group.add_option(
            "--dataclass-xpaths",
            dest="dataclass_xpaths",
            action="store_true",
            default=False,
            help="Emit each class's absolute schema path as a "
            "_yang_schema_path ClassVar and generate a data_path(root, "
            "node) function computing instance paths with list-key "
            "predicates",
        )
        group.add_option(
            "--dataclass-origin-comments",
            dest="dataclass_origin_comments",
            action="store_true",
            default=False,
            help="Emit a comment above each generated class/field whose "
            "node was contributed via a grouping (uses) or an augment, "
            "naming the defining file:line and the uses/augment site -- "
            "useful for augment-heavy schemas where it is otherwise hard "
            "to see where a node comes from",
        )
        group.add_option(
            "--dataclass-split-dir",
            dest="dataclass_split_dir",
            default=None,
            metavar="DIR",
            help="Write the bindings as a Python package under DIR (one "
            "file per YANG module that defines data nodes) instead of a "
            "single module on -o/stdout. Shared code is not duplicated: "
            "the embedded runtime goes to _runtime.py, reusable types "
            "(aliases, bits classes, identity maps) to _types.py, and "
            "__init__.py re-exports everything so `import <package>` "
            "exposes the same names as a single-file build",
        )
        group.add_option(
            "--no-dataclass-must-when",
            dest="no_dataclass_must_when",
            action="store_true",
            default=False,
            help="Do not emit YANG must/when XPath constraints into the "
            "field metadata; validate_tree() then skips them "
            "(must/when evaluation is generated by default)",
        )
        group.add_option(
            "--no-dataclass-annotations",
            dest="no_dataclass_annotations",
            action="store_true",
            default=False,
            help="Ignore RFC 7952 md:annotation statements in the "
            "compiled modules: no annotation registry is generated, so "
            "annotate()/annotations() reject every annotation name and "
            "serde rejects '@' metadata members "
            "(annotation support is generated by default)",
        )
        group.add_option(
            "--no-dataclass-native-ip-types",
            dest="no_dataclass_native_ip_types",
            action="store_true",
            default=False,
            help="Type ietf-inet-types (RFC 6991) address/prefix leaves "
            "as pattern-checked strings instead of the stdlib ipaddress "
            "classes (IPv4Address/IPv6Address/IPv4Network/IPv6Network). "
            "Native types are the default; note they cannot represent an "
            "IPv4 zone index (IPv6 zones map onto IPv6Address.scope_id)",
        )
        group.add_option(
            "--dataclass-native-type",
            dest="dataclass_native_types",
            action="append",
            default=[],
            metavar="[MODULE:]TYPEDEF=CLASS[,CLASS...]",
            help="Map a YANG typedef to native Python class(es) the "
            "type's canonical string form round-trips through "
            "(constructor accepts the string, str() produces it back), "
            "e.g. my-types:ip-and-prefix=ipaddress.IPv4Interface,"
            "ipaddress.IPv6Interface. Several classes form a union; "
            "without MODULE: the typedef name matches in any module. "
            "Repeatable. Takes precedence over the built-in "
            "ietf-inet-types mapping",
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
            with_must_when=not getattr(ctx.opts, "no_dataclass_must_when", False),
            with_origin_comments=getattr(ctx.opts, "dataclass_origin_comments", False),
            with_serde=getattr(ctx.opts, "dataclass_serde", False),
            with_xpaths=getattr(ctx.opts, "dataclass_xpaths", False),
            split_dir=getattr(ctx.opts, "dataclass_split_dir", None),
            with_annotations=not getattr(ctx.opts, "no_dataclass_annotations", False),
            with_native_ip_types=not getattr(
                ctx.opts, "no_dataclass_native_ip_types", False
            ),
            native_type_hints=getattr(ctx.opts, "dataclass_native_types", None),
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
    """Map each identity statement -> (sorted value spellings of everything
    transitively derived from it, spelling -> RFC 7951 canonical
    `module-name:identity` map). Spellings cover bare and module-prefixed
    forms; the canonical form qualifies by module *name* (RFC 7951), which
    is not necessarily the prefix."""
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
        # the RFC 7951 canonical module-qualified form, the YANG-prefix
        # form (assignment-time convenience) and the bare name
        yield "%s:%s" % (_Emitter._module_name(ident), ident.arg)
        prefix_stmt = ident.i_module.search_one("prefix")
        if prefix_stmt is not None:
            yield "%s:%s" % (prefix_stmt.arg, ident.arg)
        yield ident.arg

    values = {}
    canonical = {}
    for base_id in list(direct_derived):
        seen, out, stack = set(), set(), list(direct_derived.get(base_id, []))
        canon = {}
        while stack:
            ident = stack.pop()
            if id(ident) in seen:
                continue
            seen.add(id(ident))
            rfc7951 = "%s:%s" % (_Emitter._module_name(ident), ident.arg)
            for spelling in spellings(ident):
                out.add(spelling)
                canon[spelling] = rfc7951
            stack.extend(direct_derived.get(id(ident), []))
        values[base_id] = sorted(out)
        canonical[base_id] = canon

    # canonical identity -> tuple of DIRECT base canonicals, for the
    # XPath engine's transitive derived-from(-or-self)
    bases = {}
    for ident in identities.values():
        parents = []
        for base in ident.search("base"):
            target = getattr(base, "i_identity", None)
            if target is not None:
                parents.append(
                    "%s:%s" % (_Emitter._module_name(target), target.arg)
                )
        if parents:
            bases["%s:%s" % (_Emitter._module_name(ident), ident.arg)] = tuple(
                sorted(parents)
            )
    return values, canonical, bases


def _parse_bound(text):
    text = text.strip()
    if text in ("min", "max"):
        return None
    try:
        return int(text)
    except ValueError:
        return float(text)


# XSD Unicode category escapes (\p{...}), which Python's re module does
# not support. Outside a character class the Unicode-correct spellings
# below are substituted (libyang matches the full Unicode categories, so
# an ASCII narrowing would falsely reject values libyang accepts);
# categories with no exact stdlib-re spelling drop the whole pattern
# (unenforced rather than misjudged). INSIDE a character class only bare
# class content can be spliced in, so the ASCII approximations are used
# there -- close enough for their one real-world occurrence, RFC 6991's
# zone-id suffix classes, where zone ids are ASCII interface names.
_XSD_CATEGORY_UNICODE = {
    "L": r"[^\W\d_]",  # exactly the Unicode letters under re.UNICODE
    "Nd": r"\d",  # \d is exactly Nd under re.UNICODE
}

_XSD_CATEGORY_ASCII = {
    "L": "A-Za-z",
    "Lu": "A-Z",
    "Ll": "a-z",
    "N": "0-9",
    "Nd": "0-9",
}

_XSD_CATEGORY_RE = re.compile(r"\\p\{([A-Za-z]{1,2})\}")


def _python_pattern(arg):
    """Translate a YANG (XSD-flavored) regex into one Python's re can
    compile, or None if it can't be salvaged (that pattern is then not
    enforced rather than misjudged).

    XSD and Python regexes mostly coincide, but never exactly: XSD has
    no anchor metacharacters, so `^` and `$` are ordinary characters
    there while Python's re anchors on them -- every pattern therefore
    goes through this translation (unescaped ^/$ outside a character
    class are escaped; the negating ^ right after [ is kept). The other
    real-world construct is the Unicode category escape \\p{...}, which
    re lacks: outside a character class it is rewritten to the
    Unicode-correct re spelling where one exists (see
    _XSD_CATEGORY_UNICODE) and the pattern is dropped otherwise; inside
    a character class only ASCII approximations can be spliced in (see
    _XSD_CATEGORY_ASCII). Patterns using anything else re can't compile
    -- including negated \\P{...} categories -- are dropped (unenforced
    rather than misjudged)."""
    out = []
    pos = 0
    in_class = False
    length = len(arg)
    while pos < length:
        char = arg[pos]
        if char == "\\" and pos + 1 < length:
            nxt = arg[pos + 1]
            if nxt == "p":
                match = _XSD_CATEGORY_RE.match(arg, pos)
                if not match:
                    return None
                category = match.group(1)
                if in_class:
                    replacement = _XSD_CATEGORY_ASCII.get(category)
                else:
                    replacement = _XSD_CATEGORY_UNICODE.get(category)
                if not replacement:
                    return None
                out.append(replacement)
                pos = match.end()
                continue
            if nxt == "P":
                return None
            out.append(arg[pos : pos + 2])
            pos += 2
            continue
        if not in_class and char in "^$":
            # XSD regexes have no anchors: ^ and $ are literal there
            out.append("\\" + char)
            pos += 1
            continue
        if char == "[":
            in_class = True
            out.append(char)
            pos += 1
            # the ^ right after [ is class negation in XSD and re alike
            if pos < length and arg[pos] == "^":
                out.append("^")
                pos += 1
            continue
        if char == "]":
            in_class = False
        out.append(char)
        pos += 1
    translated = "".join(out)
    try:
        re.compile(translated)
    except re.error:
        return None
    return translated


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
    def __init__(
        self, ctx, identity_values, with_validation, with_defaults,
        with_origin_comments=False, with_serde=False, with_xpaths=False,
        identity_canonical=None, with_must_when=True, with_native_ip_types=True,
        native_type_hints=None,
    ):
        self.ctx = ctx
        self.identity_values = identity_values
        self.identity_canonical = identity_canonical or {}
        self.with_validation = with_validation
        self.with_defaults = with_defaults
        # must/when metadata only matters to validate_tree
        self.with_must_when = with_must_when and with_validation
        self.with_origin_comments = with_origin_comments
        self.with_serde = with_serde
        self.with_xpaths = with_xpaths
        self.with_native_ip_types = with_native_ip_types
        # (module-or-None, typedef) -> native class paths, from the
        # --dataclass-native-type options (generator-side input only,
        # never YANG metadata)
        self.native_type_hints = _parse_native_type_hints(native_type_hints)
        # The schema metadata table is emitted whenever some feature needs it.
        self.with_meta = with_validation or with_serde or with_xpaths
        # Names of the modules classes are emitted for; leafrefs whose
        # target lies outside these trees cannot be checked and get no
        # `leafref` metadata. Filled in by build_dataclasses.
        self.emitted_module_names = set()
        self.lines = []
        self.uses_decimal = False
        # top-level packages the emitted annotations/metadata reference
        # ("ipaddress" for the built-in inet mapping, plus whatever
        # native-type hints name)
        self.native_imports = set()
        # Module-level reusable types (Literal aliases and bits dataclasses),
        # emitted once between the imports/runtime and the tree classes and
        # referenced by name from every use site.
        self.reusable_lines = []
        self.reusable_by_key = {}  # dedup key -> assigned Python name
        self.reusable_names = set()  # every assigned name, for collision avoidance
        # The `_YANG_ANNOTATIONS.update({...})` source registering the
        # compiled modules' RFC 7952 md:annotation statements; emitted
        # after the reusable types. Empty when there are none (or
        # --no-dataclass-annotations was given).
        self.annotation_lines = []

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

    def _native_inet(self, type_stmt):
        """The _INET_NATIVE_TYPES entry -- (annotation, check bases,
        encode tag) -- of a type whose typedef chain passes through one
        of the ietf-inet-types address/prefix typedefs, or None (always
        None with --no-dataclass-native-ip-types)."""
        if not self.with_native_ip_types:
            return None
        for level in self._typedef_chain(type_stmt):
            typedef = getattr(level, "i_typedef", None)
            if (
                typedef is not None
                and typedef.arg in _INET_NATIVE_TYPES
                and self._module_name(typedef) == "ietf-inet-types"
            ):
                self.native_imports.add("ipaddress")
                return _INET_NATIVE_TYPES[typedef.arg]
        return None

    def _native_hint(self, type_stmt):
        """Dotted Python class paths a --dataclass-native-type option
        mapped onto a typedef in this type's typedef chain, as a tuple,
        or None. Takes precedence over the built-in ietf-inet-types
        mapping and is honored regardless of
        --no-dataclass-native-ip-types (it was requested
        explicitly)."""
        if not self.native_type_hints:
            return None
        for level in self._typedef_chain(type_stmt):
            typedef = getattr(level, "i_typedef", None)
            if typedef is None:
                continue
            paths = self.native_type_hints.get(
                (self._module_name(typedef), typedef.arg)
            ) or self.native_type_hints.get((None, typedef.arg))
            if paths:
                self.native_imports.update(
                    path.rpartition(".")[0] for path in paths if "." in path
                )
                return paths
        return None

    def _collect_natives(self, type_stmt, node, depth=0):
        """Every native-type-hinted class reachable from this type
        (through typedefs and union members), for the serde decode
        candidates in _FieldMeta.natives."""
        if depth > 16:
            return ()
        hint = self._native_hint(type_stmt)
        if hint is not None:
            return hint
        t = self._resolve_typedef_chain(type_stmt)
        if t.arg == "union":
            out = []
            for member in t.search("type"):
                for path in self._collect_natives(member, None, depth + 1):
                    if path not in out:
                        out.append(path)
            return tuple(out)
        if t.arg == "leafref":
            target = self._leafref_target(node, depth)
            if target is not None:
                return self._collect_natives(target.search_one("type"), target, depth + 1)
        return ()

    def annotation(self, type_stmt, node, depth=0):
        """Python annotation string for a YANG `type` statement.

        `node` is the leaf/leaf-list carrying the type (needed for the
        resolved leafref target pyang stores on the node, not the type).
        """
        if depth > 16:  # defensive: leafref chains can in theory loop
            return "str"
        hint = self._native_hint(type_stmt)
        if hint is not None:
            return " | ".join(hint)
        native = self._native_inet(type_stmt)
        if native is not None:
            return native[0]
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

    # ---- origin comments ---------------------------------------------------

    @staticmethod
    def _origin_comment(stmt):
        """One-line provenance comment for a schema node contributed via a
        grouping (`uses`) or an `augment`, or None for locally-defined
        nodes (no noise for the common case)."""

        def location(pos):
            return "%s:%d" % (os.path.basename(pos.ref), pos.line)

        details = []
        for uses in getattr(stmt, "i_uses", None) or []:
            details.append("via uses %s at %s" % (uses.arg, location(uses.pos)))
        augment = getattr(stmt, "i_augment", None)
        if augment is not None:
            details.append("via augment at %s" % location(augment.pos))
        if not details:
            return None
        return "# from %s, %s" % (location(stmt.pos), ", ".join(details))

    # ---- schema metadata --------------------------------------------------

    @staticmethod
    def _module_name(stmt):
        """RFC 7951 name qualifier of a statement: the name of the (main)
        module that contributed it; submodules resolve to their
        belongs-to."""
        if stmt.keyword in ("module", "submodule"):
            module = stmt
        else:
            module = getattr(stmt, "i_module", None)
            if module is None:
                return stmt.arg
        if module.keyword == "submodule":
            belongs_to = module.search_one("belongs-to")
            if belongs_to is not None:
                return belongs_to.arg
        return module.arg

    def _abs_data_path(self, stmt):
        """Absolute data-node path of a schema node, module-name-qualified
        at the top and wherever the module changes (RFC 7951 / FRR
        northbound convention). Returns (path, root_module_name)."""
        steps = []
        node = stmt
        while node is not None and node.keyword not in ("module", "submodule"):
            if node.keyword not in ("choice", "case"):  # not data nodes
                steps.append(node)
            node = node.parent
        steps.reverse()
        parts, parent_module = [], None
        for step in steps:
            module = self._module_name(step)
            parts.append("%s:%s" % (module, step.arg) if module != parent_module else step.arg)
            parent_module = module
        root_module = self._module_name(steps[0]) if steps else None
        return "/" + "/".join(parts), root_module

    def _leafref_path(self, node):
        """Absolute schema path of a leaf's resolved leafref target, or
        None when it cannot / must not be checked (unresolved,
        require-instance false, or target outside the emitted modules)."""
        ptr = getattr(node, "i_leafref_ptr", None)
        if ptr is None:
            return None
        for level in self._typedef_chain(node.search_one("type")):
            require = level.search_one("require-instance")
            if require is not None and require.arg == "false":
                return None
        path, root_module = self._abs_data_path(ptr[0])
        if root_module not in self.emitted_module_names:
            return None
        return path

    @staticmethod
    def _unique_leaf_path(list_stmt, part):
        """One `unique` argument resolved to a tuple of generated field
        names (descendant containers then the leaf), or None when it
        cannot be resolved (the group is then not checked). Choice and
        case steps in the schema node identifier are consumed without
        contributing a field -- the dataclasses flatten them away."""
        node = list_stmt
        fields = []
        for step in part.split("/"):
            name = step.split(":")[-1]
            child = next(
                (
                    c
                    for c in getattr(node, "i_children", []) or []
                    if c.arg == name
                ),
                None,
            )
            if child is None:
                return None
            if child.keyword in ("choice", "case"):
                node = child
                continue
            if child.keyword not in ("container", "leaf"):
                return None
            fields.append(safe_name(name))
            node = child
        if not fields or node.keyword != "leaf":
            return None
        return tuple(fields)

    def _leafref_pred_must(self, node):
        """Synthesized must expression enforcing an instance-scoped
        leafref: the plain schema-path membership check (meta.leafref)
        pools target values across the WHOLE tree, so both a predicated
        path (`../x[k=current()/../y]/name`) and a plain relative path
        (`../srv/name`, whose target set differs per parent instance)
        would accept values from unrelated instances. RFC 7950 9.9
        evaluates the path from the particular leaf instance, so every
        relative or predicated leafref becomes a must checking that the
        path's node-set (evaluated by the XPath engine from the leaf)
        contains the leaf's value. None for absolute predicate-free
        paths (the schema-path check is already exact there), for
        unresolved / require-instance-false leafrefs, and for paths
        outside the evaluated XPath subset (those keep only the
        superset schema-path check -- never misjudged)."""
        if getattr(node, "i_leafref_ptr", None) is None:
            return None
        path_stmt = None
        for level in self._typedef_chain(node.search_one("type")):
            require = level.search_one("require-instance")
            if require is not None and require.arg == "false":
                return None
            path_stmt = level.search_one("path") or path_stmt
        if path_stmt is None:
            return None
        arg = path_stmt.arg
        if "[" not in arg and not arg.strip().startswith(".."):
            return None  # absolute and predicate-free: exact already
        source = self._xpath_source(path_stmt)
        if source is None:
            return None
        return "(%s) = current()" % source

    # ---- must / when constraints ----------------------------------------

    def _xpath_source(self, stmt):
        """Prefix-normalized text of a must/when XPath expression --
        prefixes rewritten to module names, matching the name-matching
        convention of the generated evaluator -- or None when the
        expression falls outside the evaluated subset (explicit axes,
        attributes, variables, unimplemented functions, prefixes that
        don't resolve or that point at modules outside the emitted set).
        Skipped constraints are simply not enforced, never misjudged."""
        module = getattr(stmt, "i_orig_module", None) or stmt.top
        prefixes = getattr(module, "i_prefixes", None) or {}
        parts = re.split(r"""("[^"]*"|'[^']*')""", stmt.arg)
        out = []
        for index, part in enumerate(parts):
            if index % 2:  # a string literal: keep verbatim
                out.append(part)
                continue
            if "::" in part or "@" in part or "$" in part:
                return None
            for call in re.finditer(r"([A-Za-z_][\w.-]*)\s*\(", part):
                if call.group(1) not in _XPATH_SUPPORTED_FUNCTIONS:
                    return None
            unresolved = []

            def rewrite(match):
                mapped = prefixes.get(match.group(1))
                if mapped is None or mapped[0] not in self.emitted_module_names:
                    unresolved.append(match.group(1))
                    return match.group(0)
                return "%s:" % mapped[0]

            part = re.sub(r"([A-Za-z_][\w.-]*):(?=[A-Za-z_*])", rewrite, part)
            if unresolved:
                return None
            out.append(re.sub(r"\s+", " ", part))
        return "".join(out)

    def _constraint_exprs(self, child, when_stmts=()):
        """(musts, whens) metadata tuples for one data node. musts are
        (expression, error-message-or-None) pairs; whens are
        (expression, context-is-self) pairs -- a `when` written on the
        node itself has the node as XPath context, one inherited from a
        uses / augment / choice / case has the parent (RFC 7950
        7.21.5). Constraints outside the evaluated subset are dropped
        (see _xpath_source)."""
        musts = []
        for must in child.search("must"):
            expression = self._xpath_source(must)
            if expression is None:
                continue
            error_message = must.search_one("error-message")
            musts.append(
                (expression, error_message.arg if error_message is not None else None)
            )
        whens = []

        def add_when(stmt, self_context):
            if stmt is None:
                return
            expression = self._xpath_source(stmt)
            if expression is not None and (expression, self_context) not in whens:
                whens.append((expression, self_context))

        for when in child.search("when"):
            # pyang copies a uses' when onto every expanded child (marked
            # i_origin='uses'); those keep the parent as context node
            add_when(when, getattr(when, "i_origin", None) != "uses")
        for uses in getattr(child, "i_uses", None) or []:
            add_when(uses.search_one("when"), False)
        augment = getattr(child, "i_augment", None)
        if augment is not None:
            add_when(augment.search_one("when"), False)
        for stmt in when_stmts:  # choice/case levels flattened away
            add_when(stmt, False)
        return tuple(musts), tuple(whens)

    def _encode_tag(self, type_stmt, node, depth=0):
        """IETF-JSON value encoding tag for types that do not encode as
        their natural Python/JSON value, or None."""
        if depth > 16:
            return None
        if self._native_hint(type_stmt) is not None:
            return "native"
        native = self._native_inet(type_stmt)
        if native is not None:
            return native[2]
        t = self._resolve_typedef_chain(type_stmt)
        if t.arg == "leafref":
            target = self._leafref_target(node, depth)
            if target is not None:
                return self._encode_tag(target.search_one("type"), target, depth + 1)
            return None
        if t.arg in ("int64", "uint64"):
            return "int64"  # RFC 7951: 64-bit ints are JSON strings
        if t.arg == "decimal64":
            return "decimal"
        if t.arg == "empty":
            return "empty"
        if t.arg == "binary":
            return "binary"
        if t.arg == "identityref":
            return "identityref"
        if t.arg == "bits" and t.search("bit"):
            return "bits"
        if t.arg == "union" and self._union_has_native_member(t, depth):
            return "ip-union"
        return None

    def _union_has_native_member(self, resolved_union, depth):
        """Whether a union mixes native (ipaddress or native-type-hinted)
        members with other types (its values then need the ip-union
        serde coercion)."""
        if depth > 16:
            return False
        for member in resolved_union.search("type"):
            if self._native_inet(member) is not None:
                return True
            if self._native_hint(member) is not None:
                return True
            inner = self._resolve_typedef_chain(member)
            if inner.arg == "union" and self._union_has_native_member(inner, depth + 1):
                return True
        return False

    def _identityref_map_name(self, type_stmt, node, depth=0):
        """For an identityref leaf (possibly via typedefs/leafref): the name
        of a hoisted module-level dict mapping every accepted spelling to
        the RFC 7951 canonical `module-name:identity` form. None when not
        an identityref (or serde is off)."""
        if not self.with_serde or depth > 16:
            return None
        t = self._resolve_typedef_chain(type_stmt)
        if t.arg == "leafref":
            target = self._leafref_target(node, depth)
            if target is None:
                return None
            return self._identityref_map_name(target.search_one("type"), target, depth + 1)
        if t.arg != "identityref":
            return None
        base = t.search_one("base")
        identity = getattr(base, "i_identity", None) if base is not None else None
        if identity is None:
            return None
        mapping = self.identity_canonical.get(id(identity))
        if not mapping:
            return None

        def build(name):
            lines = ["%s = {" % name]
            lines.extend("    %r: %r," % (k, mapping[k]) for k in sorted(mapping))
            lines.append("}")
            return lines

        return self._register_reusable(
            ("identity-map", id(identity)),
            "_%sIdentities" % class_name(identity.arg),
            build,
        )

    def field_meta_expr(self, child, case, cls_name=None, when_stmts=()):
        """`_FieldMeta(...)` constructor source for one field."""
        kind = child.keyword
        args = [repr(child.arg), repr(self._module_name(child)), repr(kind)]
        if cls_name is not None:
            args.append("cls=%s" % cls_name)
        scoped_must = (
            self._leafref_pred_must(child)
            if self.with_must_when and kind in ("leaf", "leaf-list")
            else None
        )
        if kind in ("leaf", "leaf-list"):
            if self.with_validation:
                check = self.check_expr(child.search_one("type"), child)
                if check is not None:
                    args.append("check=%s" % check)
            encode = self._encode_tag(child.search_one("type"), child)
            if encode is not None:
                args.append("encode=%r" % encode)
            if encode in ("native", "ip-union"):
                natives = self._collect_natives(child.search_one("type"), child)
                if natives:
                    args.append("natives=(%s,)" % ", ".join(natives))
            identity_map = self._identityref_map_name(child.search_one("type"), child)
            if identity_map is not None:
                args.append("identity_map=%s" % identity_map)
            leafref = self._leafref_path(child)
            if leafref is not None and scoped_must is None:
                # the synthesized instance-scoped must supersedes the
                # whole-tree schema-path membership check
                args.append("leafref=%r" % leafref)
        if kind == "leaf":
            mandatory = child.search_one("mandatory")
            if mandatory is not None and mandatory.arg == "true":
                args.append("mandatory=True")
        if kind == "container" and child.search_one("presence") is not None:
            args.append("presence=True")
        if kind in ("list", "leaf-list"):
            min_elements = child.search_one("min-elements")
            if min_elements is not None:
                args.append("min_elements=%d" % int(min_elements.arg))
            max_elements = child.search_one("max-elements")
            if max_elements is not None and max_elements.arg != "unbounded":
                args.append("max_elements=%d" % int(max_elements.arg))
        if kind == "list":
            key = child.search_one("key")
            if key is not None:
                args.append("keys=%r" % (tuple(safe_name(k) for k in key.arg.split()),))
            unique_groups = []
            for unique in child.search("unique"):
                group = []
                for part in unique.arg.split():
                    leaf_path = self._unique_leaf_path(child, part)
                    if leaf_path is None:
                        group = None  # unresolvable: skip the whole group
                        break
                    group.append(leaf_path)
                if group:
                    unique_groups.append(tuple(group))
            if unique_groups:
                args.append("unique=%r" % (tuple(unique_groups),))
        if case is not None:
            args.append("case=%r" % (case,))
        if self.with_must_when:
            musts, whens = self._constraint_exprs(child, when_stmts)
            if scoped_must is not None:
                musts = musts + (
                    (
                        scoped_must,
                        "leafref has no target instance with this value",
                    ),
                )
            if musts:
                args.append("musts=%r" % (musts,))
            if whens:
                args.append("whens=%r" % (whens,))
        return "_FieldMeta(%s)" % ", ".join(args)

    def emit_annotation_registry(self, annotation_stmts):
        """`_YANG_ANNOTATIONS.update({...})` source registering every RFC
        7952 md:annotation statement found in the compiled modules, keyed
        by python-safe annotation name (module-prefixed on the rare name
        collision between two modules). The value type gets the same
        check/encode treatment as a leaf of that type; an unresolvable
        type just means the value is accepted unvalidated, never
        misjudged."""
        entries = []
        taken = set()
        for module, stmt in annotation_stmts:
            name = safe_name(stmt.arg)
            if name in taken:
                name = safe_name("%s_%s" % (module.arg, stmt.arg))
            taken.add(name)
            args = ["yang_name=%r" % stmt.arg, "module=%r" % self._module_name(module)]
            type_stmt = stmt.search_one("type")
            if type_stmt is not None:
                if self.with_validation:
                    check = self.check_expr(type_stmt, stmt)
                    if check is not None:
                        args.append("check=%s" % check)
                encode = self._encode_tag(type_stmt, stmt)
                if encode is not None:
                    args.append("encode=%r" % encode)
            entries.append((name, "_AnnotationDef(%s)" % ", ".join(args)))
        self.annotation_lines.append(
            "# RFC 7952 metadata annotations defined by the compiled modules"
        )
        self.annotation_lines.append("_YANG_ANNOTATIONS.update({")
        for name, expr in entries:
            self.annotation_lines.append("    %r: %s," % (name, expr))
        self.annotation_lines.append("})")
        self.annotation_lines.append("")

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
        hint = self._native_hint(type_stmt)
        if hint is not None:
            # the isinstance check replaces the string restrictions
            return "_Check('native', natives=(%s,))" % ", ".join(hint)
        native = self._native_inet(type_stmt)
        if native is not None:
            # class checks replace the string patterns; string restrictions
            # written on outer typedef levels do not apply to native objects
            checks = ["_Check(%r)" % base for base in native[1]]
            if len(checks) == 1:
                return checks[0]
            return "_Check('union', members=(%s,))" % ", ".join(checks)
        chain = self._typedef_chain(type_stmt)
        base = chain[-1]

        ranges, lengths, patterns, inverted = [], [], [], []
        for level in chain:
            range_stmt = level.search_one("range")
            if range_stmt is not None:
                ranges.append(_parse_range_arg(range_stmt.arg))
            length_stmt = level.search_one("length")
            if length_stmt is not None:
                lengths.append(_parse_range_arg(length_stmt.arg))
            for pattern_stmt in level.search("pattern"):
                pattern = _python_pattern(pattern_stmt.arg)
                if pattern is not None:
                    modifier = pattern_stmt.search_one("modifier")
                    if modifier is not None and modifier.arg == "invert-match":
                        inverted.append(pattern)
                    else:
                        patterns.append(pattern)

        values = ()
        bits = ()
        closed = False
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
            # closed set: an empty derived set means NO value is valid
            # (libyang agrees), not "unrestricted"
            identity_names = self._identityref_values(base)
            values = tuple(identity_names)
            check_base = "str"
            closed = True
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
        if inverted:
            args.append("inverted_patterns=%r" % (tuple(inverted),))
        if values:
            args.append("values=%r" % (values,))
        if closed:
            args.append("closed=True")
        if bits:
            args.append("bits=%r" % (bits,))
        if members:
            args.append("members=(%s,)" % ", ".join(members))
        if base.arg == "decimal64":
            fraction_digits = base.search_one("fraction-digits")
            if fraction_digits is not None:
                args.append("fraction_digits=%d" % int(fraction_digits.arg))
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

    @staticmethod
    def _native_default_expr(paths, text):
        """Constructor expression for a native-type-hinted default: probe
        the alternatives at codegen time (the classes are importable
        here just as in the generated module's environment) and emit the
        first that accepts the default string; fall back to the first
        alternative when none can be probed."""
        for path in paths:
            module_name, _, cls_name = path.rpartition(".")
            if not module_name:
                continue
            try:
                getattr(importlib.import_module(module_name), cls_name)(text)
            except Exception:
                continue
            return "%s(%r)" % (path, text)
        return "%s(%r)" % (paths[0], text)

    def _default_value_expr(self, text, type_stmt):
        hint = self._native_hint(type_stmt)
        if hint is not None:
            return self._native_default_expr(hint, text.strip())
        native = self._native_inet(type_stmt)
        if native is not None:
            if native[2] == "ip-prefix":
                return "ipaddress.ip_network(%r, strict=False)" % text.strip()
            return "ipaddress.ip_address(%r)" % text.strip()
        base = self._resolve_typedef_chain(type_stmt)
        if base.arg in _INT_BUILTIN_RANGE:
            return repr(int(text, 0))
        if base.arg == "boolean":
            return repr(text.strip() == "true")
        if base.arg == "decimal64":
            self.uses_decimal = True
            return "decimal.Decimal(%r)" % text.strip()
        if base.arg == "binary":
            # the YANG default is the base64 text; the field holds bytes
            try:
                return repr(base64.b64decode(text.strip(), validate=True))
            except (ValueError, binascii.Error):
                return repr(text)  # malformed schema default: leave as-is
        if base.arg == "union":
            try:
                return repr(int(text, 0))
            except ValueError:
                return repr(text)
        if base.arg == "identityref":
            return repr(self._normalized_identity_spelling(text, base))
        return repr(text)

    def _normalized_identity_spelling(self, text, resolved_type_stmt):
        """Preferred spelling of an identityref default: bare unless the
        bare name is claimed by a different identity. A YANG default may
        spell the identity with the *importing* module's prefix, which the
        spelling map (keyed on the defining module's prefix + bare) does
        not contain -- fall back to the bare name to find it."""
        base = resolved_type_stmt.search_one("base")
        identity = getattr(base, "i_identity", None) if base is not None else None
        mapping = self.identity_canonical.get(id(identity), {})
        canonical = mapping.get(text) or mapping.get(text.split(":")[-1])
        if canonical is None:
            return text
        accepted = [s for s, c in mapping.items() if c == canonical]
        return min(accepted, key=lambda s: (":" in s, s))

    # ---- tree walking ---------------------------------------------------

    @staticmethod
    def _flattened_children(stmt):
        """Config-true data children with choice/case flattened away.
        Returns (entries, choices): entries as (child, case, when_stmts)
        triples where `case` is the chain of (choice, case) pairs a
        child was flattened out of, outermost first (or None), and
        `when_stmts` the `when` statements of the flattened-away
        choice/case levels; choices as choice name -> (mandatory,
        chain-of-enclosing-(choice, case)-pairs) -- a nested choice's
        mandatory-ness applies only while its enclosing case is the
        selected one."""
        entries, choices = [], {}
        for child in getattr(stmt, "i_children", []) or []:
            if child.keyword not in _DATA_KEYWORDS:
                continue
            if not getattr(child, "i_config", True):
                continue
            if child.keyword == "choice":
                mandatory = child.search_one("mandatory")
                choices[child.arg] = (
                    mandatory is not None and mandatory.arg == "true",
                    (),
                )
                choice_whens = tuple(
                    w for w in (child.search_one("when"),) if w is not None
                )
                for case in getattr(child, "i_children", []) or []:
                    if case.keyword == "case":
                        tag = ((child.arg, case.arg),)
                        case_whens = choice_whens + tuple(
                            w for w in (case.search_one("when"),) if w is not None
                        )
                        sub_entries, sub_choices = _Emitter._flattened_children(case)
                        entries.extend(
                            (sub, tag + (sub_case or ()), case_whens + sub_whens)
                            for sub, sub_case, sub_whens in sub_entries
                        )
                        choices.update(
                            (name, (sub_mandatory, tag + sub_chain))
                            for name, (sub_mandatory, sub_chain) in sub_choices.items()
                        )
                    elif case.keyword in _DATA_KEYWORDS:  # shorthand case
                        entries.append(
                            (case, ((child.arg, case.arg),), choice_whens)
                        )
            else:
                entries.append((child, None, ()))
        return entries, choices

    @staticmethod
    def _data_children(stmt):
        """Config-true data children, with choice/case flattened away."""
        return [child for child, _, _ in _Emitter._flattened_children(stmt)[0]]

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
        bits = [(safe_name(bit.arg), bit.arg) for bit in resolved_type.search("bit")]
        bits_module = self._module_name(resolved_type)

        def build(name):
            base = "(_YangNode)" if self.with_validation else ""
            out = [
                "@dataclasses.dataclass",
                "class %s%s:" % (name, base),
                '    """Bits `%s`: one bool per YANG bit; truthy iff any bit is set."""'
                % label,
            ]
            out.extend("    %s: bool = False" % bit_fname for bit_fname, _ in bits)
            if self.with_meta:
                out.append("")
                out.append("    _yang_name = %r" % label)
                out.append("    _yang_module = %r" % bits_module)
                out.append("    _yang_fields = {")
                check = ", check=_Check('bool')" if self.with_validation else ""
                out.extend(
                    "        %r: _FieldMeta(%r, %r, 'bit'%s),"
                    % (bit_fname, bit_yang, bits_module, check)
                    for bit_fname, bit_yang in bits
                )
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
        field_metas = []
        entries, choices = self._flattened_children(stmt)
        for child, case, when_stmts in entries:
            fname = safe_name(child.arg)
            if self.with_origin_comments:
                comment = self._origin_comment(child)
                if comment is not None:
                    self.lines.append(body_indent + comment)
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
                field_metas.append(
                    (fname, self.field_meta_expr(child, case, child_cname, when_stmts))
                )
            else:  # leaf / leaf-list
                resolved = self._resolve_typedef_chain(child.search_one("type"))
                if resolved.arg == "bits" and resolved.search("bit"):
                    bits_cname = self._register_bits_class(
                        child.search_one("type"), resolved, child
                    )
                    if child.keyword == "leaf-list":
                        self.lines.append(
                            "%s%s: list[%s] = dataclasses.field(default_factory=list)"
                            % (body_indent, fname, bits_cname)
                        )
                    else:
                        factory = self._bits_default_factory(child, bits_cname)
                        self.lines.append(
                            "%s%s: %s = dataclasses.field(default_factory=%s)"
                            % (body_indent, fname, bits_cname, factory)
                        )
                    self.lines.append("")
                    field_metas.append(
                        (fname, self.field_meta_expr(child, case, bits_cname, when_stmts))
                    )
                    continue
                self._emit_leaf(child, fname, body_indent)
                field_metas.append(
                    (fname, self.field_meta_expr(child, case, when_stmts=when_stmts))
                )

        if self.with_meta:
            self.lines.append("")
            self.lines.append("%s_yang_name = %r" % (body_indent, stmt.arg))
            self.lines.append("%s_yang_module = %r" % (body_indent, self._module_name(stmt)))
            if self.with_xpaths:
                if stmt.keyword in ("module", "submodule"):
                    schema_path = "/"
                else:
                    schema_path, _ = self._abs_data_path(stmt)
                self.lines.append("%s_yang_schema_path = %r" % (body_indent, schema_path))
            if choices:
                self.lines.append("%s_yang_choices = %r" % (body_indent, choices))
            if field_metas:
                self.lines.append("%s_yang_fields = {" % body_indent)
                for fname, meta in field_metas:
                    self.lines.append("%s    %r: %s," % (body_indent, fname, meta))
                self.lines.append("%s}" % body_indent)

        if len(self.lines) == body_start:
            self.lines.append("%spass" % body_indent)
        self.lines.append("")


def _collect_annotation_stmts(ctx):
    """Every RFC 7952 md:annotation statement in the compilation set, as
    (defining module, statement) pairs in a stable order. Scans all
    modules pyang loaded (like identities), so annotations defined by an
    imported module are registered too."""
    seen = set()
    found = []
    for module in ctx.modules.values():
        if id(module) in seen:
            continue
        seen.add(id(module))
        for stmt in module.substmts:
            if stmt.keyword == ("ietf-yang-metadata", "annotation"):
                found.append((module, stmt))
    found.sort(key=lambda pair: (pair[0].arg, pair[1].arg))
    return found


def build_dataclasses(
    ctx, modules, fd, with_validation=True, with_defaults=True,
    with_origin_comments=False, with_serde=False, with_xpaths=False,
    split_dir=None, with_must_when=True, with_annotations=True,
    with_native_ip_types=True, native_type_hints=None,
):
    identity_values, identity_canonical, identity_bases = _build_identity_values(ctx)

    emitter = _Emitter(
        ctx, identity_values, with_validation, with_defaults,
        with_origin_comments=with_origin_comments, with_serde=with_serde,
        with_xpaths=with_xpaths, identity_canonical=identity_canonical,
        with_must_when=with_must_when, with_native_ip_types=with_native_ip_types,
        native_type_hints=native_type_hints,
    )
    # Reserve the top-level module class names so a reusable type (emitted in
    # the same module namespace) can never shadow one of them.
    emitter.reusable_names.update(class_name(m.arg) for m in modules)
    data_modules = [m for m in modules if _Emitter._data_children(m)]
    emitter.emitted_module_names = {m.arg for m in data_modules}
    if with_annotations and emitter.with_meta:
        annotation_stmts = _collect_annotation_stmts(ctx)
        if annotation_stmts:
            emitter.emit_annotation_registry(annotation_stmts)
    if emitter.with_meta and with_must_when and identity_bases:
        emitter.lines.append("_YANG_IDENTITY_BASES.update({")
        for canonical in sorted(identity_bases):
            emitter.lines.append(
                "    %r: %r," % (canonical, identity_bases[canonical])
            )
        emitter.lines.append("})")
        emitter.lines.append("")
    segments = []  # (module, its slice of emitter.lines), for split mode
    for module in data_modules:
        start = len(emitter.lines)
        emitter.emit_node_class(module, class_name(module.arg), "")
        segments.append((module, emitter.lines[start:]))
    if not data_modules:
        emitter.lines.append("# (none of the input modules define config data nodes)")

    doc = [
        '"""Typed dataclass bindings generated by pyangbind (pybind-dataclass plugin).',
        "",
        "Source YANG modules: %s." % ", ".join(sorted(m.arg for m in modules)),
        "Generated with: validation=%s, defaults=%s, serde=%s."
        % (with_validation, with_defaults, with_serde),
        "Do not edit by hand -- regenerate instead.",
        '"""',
    ]

    if split_dir is not None:
        _write_split_package(
            emitter, doc, data_modules, segments, split_dir,
            with_validation, with_serde, with_xpaths,
        )
        fd.write("# multi-file bindings package written to %s\n" % split_dir)
        return

    header = doc + [
        "",
        "from __future__ import annotations",
        "",
        "import dataclasses",
        "import typing",
    ]
    if with_serde:
        header.append("import base64")
    if with_validation:
        header.append("import re")
    if emitter.uses_decimal or with_validation or with_serde:
        header.append("import decimal")
    # serde's union fallback isinstance-checks ipaddress types even when
    # no native leaf was emitted; native-type hints add their own packages
    packages = set(emitter.native_imports)
    if with_serde:
        packages.add("ipaddress")
    header.extend("import %s" % pkg for pkg in sorted(packages))
    if emitter.with_meta:
        header.append(_META_RUNTIME.rstrip())
    if with_validation:
        header.append(_XPATH_EVAL_RUNTIME.rstrip())
        header.append(_VALIDATION_RUNTIME.rstrip())
    if with_serde:
        header.append(_SERDE_RUNTIME.rstrip())
    if with_xpaths:
        header.append(_XPATH_RUNTIME.rstrip())
    header.extend(["", ""])

    # Reusable types (Literal aliases, bits dataclasses) go between the
    # imports/runtime and the tree classes so forward references resolve and
    # bits classes exist before they are used as field factory defaults.
    # The annotation registry follows them (it may reference _Check).
    body = emitter.reusable_lines + emitter.annotation_lines + emitter.lines
    fd.write("\n".join(header + body))
    if not body or body[-1] != "":
        fd.write("\n")


def _write_split_package(
    emitter, doc, data_modules, segments, split_dir,
    with_validation, with_serde, with_xpaths,
):
    """Write the bindings as a Python package, one file per data module.

    Everything shared lives exactly once: the embedded runtime (metadata
    dataclass, validation, serde, xpaths) in ``_runtime.py`` and every
    reusable type (Literal aliases, bits dataclasses, identity maps) in
    ``_types.py``. Per-module files hold only that module's tree classes;
    ``__init__.py`` re-exports the lot, so ``import <package>`` exposes
    the same names as a single-file build. (The classic pybind backend's
    --split-class-dir needs no equivalent of _runtime.py only because its
    runtime is the pyangbind.lib dependency; this backend stays
    stdlib-only.)"""

    def needed(lines, names):
        return [n for n in names if any(n in line for line in lines)]

    def write(filename, lines):
        path = os.path.join(split_dir, filename)
        with open(path, "w") as out:
            out.write("\n".join(lines))
            if not lines or lines[-1] != "":
                out.write("\n")

    os.makedirs(split_dir, exist_ok=True)

    runtime_blocks = []
    if emitter.with_meta:
        runtime_blocks.append(_META_RUNTIME.rstrip())
    if with_validation:
        runtime_blocks.append(_XPATH_EVAL_RUNTIME.rstrip())
        runtime_blocks.append(_VALIDATION_RUNTIME.rstrip())
    if with_serde:
        runtime_blocks.append(_SERDE_RUNTIME.rstrip())
    if with_xpaths:
        runtime_blocks.append(_XPATH_RUNTIME.rstrip())
    if runtime_blocks:
        header = [
            '"""Shared runtime for the generated bindings package."""',
            "",
            "import dataclasses",
            "import typing",
        ]
        if with_serde:
            header.append("import base64")
        if with_validation:
            header.append("import re")
        if with_validation or with_serde:
            header.append("import decimal")
        if with_serde or "ipaddress" in emitter.native_imports:
            header.append("import ipaddress")
        write("_runtime.py", header + runtime_blocks + [""])

    # Names generated code may reference from _runtime, private + public.
    runtime_private = (
        ["_FieldMeta", "_AnnotationDef", "_YANG_ANNOTATIONS"]
        if emitter.with_meta
        else []
    ) + (["_Check", "_YangNode"] if with_validation else [])
    runtime_public = (
        (["YangValidationError", "validate_tree"] if with_validation else [])
        + (["to_ietf_json", "from_ietf_json"] if with_serde else [])
        + (["data_path"] if with_xpaths else [])
        + (["annotate", "annotations"] if emitter.annotation_lines else [])
    )

    reusable_names = list(emitter.reusable_by_key.values())
    types_lines = emitter.reusable_lines + emitter.annotation_lines
    if types_lines:
        header = [
            '"""Reusable types shared by the generated bindings package',
            "(type aliases, bits dataclasses, identityref spelling maps,",
            "the RFC 7952 annotation registry).",
            '"""',
            "",
            "import dataclasses",
            "import typing",
        ]
        from_runtime = needed(types_lines, runtime_private)
        if from_runtime:
            header.append("from ._runtime import %s" % ", ".join(from_runtime))
        header += [
            "",
            "__all__ = [",
            *("    %r," % n for n in reusable_names),
            "]",
            "",
        ]
        write("_types.py", header + types_lines)

    module_files = {}  # module file stem -> root class name
    for module, lines in segments:
        stem = safe_name(module.arg)
        if stem in module_files:
            raise ValueError(
                "YANG modules %r map to the same Python file name %s.py"
                % (sorted(m.arg for m, _ in segments if safe_name(m.arg) == stem), stem)
            )
        module_files[stem] = class_name(module.arg)
        header = [
            '"""Typed dataclass bindings for YANG module `%s`."""' % module.arg,
            "",
            "from __future__ import annotations",
            "",
            "import dataclasses",
        ]
        if any("typing." in line for line in lines):
            header.append("import typing")
        if any("decimal." in line for line in lines):
            header.append("import decimal")
        header.extend(
            "import %s" % pkg
            for pkg in sorted(emitter.native_imports | {"ipaddress"})
            if any(pkg + "." in line for line in lines)
        )
        from_runtime = needed(lines, runtime_private)
        if from_runtime:
            header.append("from ._runtime import %s" % ", ".join(from_runtime))
        if reusable_names:
            header.append("from ._types import *  # noqa: F401,F403")
        header += ["", ""]
        write("%s.py" % stem, header + lines)

    init = doc + [""]
    exported = []
    if runtime_public:
        init.append("from ._runtime import %s" % ", ".join(runtime_public))
        exported += runtime_public
    if types_lines:
        # Also executes the RFC 7952 annotation-registry population.
        init.append("from ._types import *  # noqa: F401,F403")
        exported += reusable_names
    for stem, cname in module_files.items():
        init.append("from .%s import %s" % (stem, cname))
        exported.append(cname)
    if not data_modules:
        init.append("# (none of the input modules define config data nodes)")
    if exported:
        init += [
            "",
            "__all__ = [",
            *("    %r," % n for n in exported),
            "]",
        ]
    write("__init__.py", init)

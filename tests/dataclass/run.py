#!/usr/bin/env python
"""Tests for the pybind-dataclass output plugin.

Standalone (does not use tests.base.PyangBindTestCase, which hardcodes
`-f pybind`): generates bindings from dataclass.yang in every flag
combination and exercises them. The generation itself is half the test:
executing the generated module would raise dataclasses' "non-default
argument follows default argument" TypeError if any field were ever
emitted without a default -- dataclass.yang deliberately interleaves
YANG-defaulted and default-less leaves to pin that down.
"""

import os.path
import shutil
import subprocess
import sys
import types
import typing
import unittest

TEST_PATH = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(os.path.dirname(TEST_PATH))
PLUGIN_DIR = os.path.join(BASE_DIR, "pyangbind", "plugin")


# The bits-in-union fixture lives with the serialise tests; reused here to
# exercise a union whose members include a bits typedef.
SERIALISE_DIR = os.path.join(BASE_DIR, "tests", "serialise", "json-serialise")


def generate(*flags, output_format="pybind-dataclass", yang_file=None, search_path=None):
    pyang = shutil.which("pyang")
    if pyang is None:
        raise RuntimeError("Could not locate `pyang` executable.")
    yang_file = yang_file or os.path.join(TEST_PATH, "dataclass.yang")
    search_path = search_path or TEST_PATH
    cmd = [
        pyang,
        "--plugindir",
        PLUGIN_DIR,
        "-f",
        output_format,
        "-p",
        search_path,
        *flags,
        yang_file,
    ]
    code = subprocess.check_output(cmd, stderr=subprocess.PIPE, env={"PYTHONPATH": BASE_DIR})
    module = types.ModuleType("dataclass_bindings")
    # Registered in sys.modules because dataclasses' ClassVar/KW_ONLY
    # string-annotation handling looks the defining module up there
    # (3.12+); a bare exec into an unregistered module crashes it.
    sys.modules[module.__name__] = module
    try:
        # Raises TypeError here if any generated field lacks a default.
        exec(compile(code, "dataclass_bindings.py", "exec"), module.__dict__)
    finally:
        del sys.modules[module.__name__]
    module._source = code.decode()
    return module


class DataclassDefaultsOnTests(unittest.TestCase):
    """Default flags: validation and YANG defaults both generated."""

    @classmethod
    def setUpClass(cls):
        cls.bindings = generate()

    def setUp(self):
        self.box = self.bindings.Dataclass.Box()

    def test_default_less_leaf_after_defaulted_leaf_generates(self):
        # dataclass.yang has plain-after directly after with-default; the
        # module executed, so no field-ordering TypeError occurred. Both
        # spellings of "unset" behave:
        self.assertIsNone(self.box.plain_before)
        self.assertIsNone(self.box.plain_after)
        self.assertIsNone(self.box.plain_trailing)

    def test_yang_defaults_applied(self):
        self.assertEqual(self.box.with_default, "lol")
        self.assertEqual(self.box.number_with_default, 42)
        self.assertIs(self.box.flag_with_default, True)
        self.assertEqual(self.box.strings_with_defaults, ["one", "two"])
        self.assertIs(self.box.bits_with_default.alpha, True)
        self.assertIs(self.box.bits_with_default.beta, False)
        self.assertIs(self.box.bits_with_default.gamma, True)
        # identityref default is normalised to the bare spelling even
        # though the YANG default statement spells it "dc:cat"
        self.assertEqual(self.box.pet_with_default, "cat")

    def test_bits_truthiness(self):
        self.assertTrue(self.box.bits_with_default)  # alpha+gamma default True
        self.box.bits_with_default.alpha = False
        self.box.bits_with_default.gamma = False
        self.assertFalse(self.box.bits_with_default)  # no bit set -> falsy

    def test_typedef_default_applied(self):
        self.assertEqual(self.box.from_typedef, 50)

    def test_mutable_defaults_not_shared_between_instances(self):
        other = self.bindings.Dataclass.Box()
        self.box.strings_with_defaults.append("three")
        self.box.bits_with_default.beta = True
        self.assertEqual(other.strings_with_defaults, ["one", "two"])
        self.assertIs(other.bits_with_default.beta, False)

    def test_validation_enabled_by_default(self):
        with self.assertRaises(self.bindings.YangValidationError):
            self.box.number_with_default = 5  # below range 10..4096
        with self.assertRaises(self.bindings.YangValidationError):
            self.box.with_default = 42  # not a string
        with self.assertRaises(self.bindings.YangValidationError):
            self.box.bits_with_default.alpha = "yes"  # bits are bools

    def test_defaults_satisfy_validation_on_init(self):
        # Constructing with only defaults must not raise.
        self.bindings.Dataclass()


class DataclassReusableAliasTests(unittest.TestCase):
    """Enum-typedef and identityref types are hoisted to module-level
    `type X = typing.Literal[...]` aliases and referenced by name; inline
    anonymous enums stay inlined."""

    @classmethod
    def setUpClass(cls):
        cls.bindings = generate()

    def test_named_enum_typedef_hoisted(self):
        # `typedef direction` -> module-level `Direction` alias.
        self.assertTrue(hasattr(self.bindings, "Direction"))
        self.assertEqual(
            set(typing.get_args(self.bindings.Direction.__value__)), {"in", "out"}
        )

    def test_named_enum_typedef_deduped(self):
        # dir-a and dir-b share the one alias, referenced by name. With
        # `from __future__ import annotations` the annotation is the source
        # string.
        anns = self.bindings.Dataclass.Box.__annotations__
        self.assertEqual(anns["dir_a"], "Direction | None")
        self.assertEqual(anns["dir_b"], "Direction | None")

    def test_identityref_aliased_by_base_identity(self):
        self.assertTrue(hasattr(self.bindings, "Animal"))
        values = set(typing.get_args(self.bindings.Animal.__value__))
        # bare, YANG-prefix and RFC 7951 module-qualified spellings of
        # every derived identity
        self.assertEqual(
            values,
            {"dog", "cat", "dc:dog", "dc:cat", "dataclass:dog", "dataclass:cat"},
        )
        self.assertEqual(self.bindings.Dataclass.Box.__annotations__["pet"], "Animal | None")

    def test_inline_anonymous_enum_not_hoisted(self):
        # `mood` is an inline enum: no module-level alias, inlined at the field.
        self.assertFalse(hasattr(self.bindings, "Mood"))
        self.assertEqual(
            self.bindings.Dataclass.Box.__annotations__["mood"],
            "typing.Literal['happy', 'sad'] | None",
        )

    def test_alias_does_not_shadow_a_tree_class(self):
        # The module class name is reserved; aliases never collide with it.
        self.assertTrue(isinstance(self.bindings.Dataclass, type))


class DataclassBitsUnionTests(unittest.TestCase):
    """bits become module-level reusable dataclasses, usable as union members
    (previously they degraded to set[str]). Exercised via the json-serialise
    fixture: `typedef bits1` appears both as a standalone leaf and inside a
    union `str | nhopenum | bits1`."""

    @classmethod
    def setUpClass(cls):
        cls.bindings = generate(
            yang_file=os.path.join(SERIALISE_DIR, "json-serialise.yang"),
            search_path=SERIALISE_DIR,
        )
        cls.L1 = cls.bindings.JsonSerialise.C1.L1

    def test_bits_hoisted_to_module_level(self):
        self.assertTrue(isinstance(self.bindings.Bits1, type))
        b = self.bindings.Bits1()
        self.assertIs(b.flag1, False)
        self.assertFalse(b)  # no bit set -> falsy
        b.flag2 = True
        self.assertTrue(b)

    def test_bits_is_a_proper_union_member(self):
        # The annotation is `list[str | Nhopenum | Bits1]`, not `... | set[str]`.
        ann = self.L1.__annotations__["next_hop"]
        self.assertIn("Bits1", ann)
        self.assertNotIn("set[str]", ann)

    def test_union_accepts_a_bits_instance(self):
        entry = self.L1()
        entry.next_hop = [self.bindings.Bits1(flag1=True)]  # validates on assignment
        entry.next_hop = ["1.2.3.4"]  # a plain string member also validates

    def test_union_rejects_value_matching_no_member(self):
        entry = self.L1()
        with self.assertRaises(self.bindings.YangValidationError):
            entry.next_hop = [123]  # not str, not the enum, not a bits instance

    def test_standalone_bits_leaf(self):
        entry = self.L1()
        self.assertIs(entry.bits.flag1, False)
        with self.assertRaises(self.bindings.YangValidationError):
            entry.bits.flag1 = "yes"  # bits fields are bools


class DataclassMetadataTests(unittest.TestCase):
    """Every generated class carries a `_yang_fields` schema metadata table
    (plus `_yang_name`/`_yang_module`/`_yang_choices` ClassVars) whenever a
    feature needs it -- currently whenever validation is generated."""

    @classmethod
    def setUpClass(cls):
        cls.bindings = generate()
        cls.top = cls.bindings.Dataclass

    def test_class_identity(self):
        self.assertEqual(self.top._yang_name, "dataclass")
        self.assertEqual(self.top._yang_module, "dataclass")
        self.assertEqual(self.top.Box._yang_name, "box")

    def test_leaf_meta_maps_back_to_yang_name(self):
        meta = self.top.Box._yang_fields["plain_before"]
        self.assertEqual(meta.yang_name, "plain-before")
        self.assertEqual(meta.module, "dataclass")
        self.assertEqual(meta.kind, "leaf")
        self.assertIsNotNone(meta.check)

    def test_container_and_list_meta_reference_nested_class(self):
        self.assertIs(self.top._yang_fields["box"].cls, self.top.Box)
        self.assertEqual(self.top._yang_fields["box"].kind, "container")
        server = self.top.Refs._yang_fields["server"]
        self.assertEqual(server.kind, "list")
        self.assertIs(server.cls, self.top.Refs.Server)

    def test_list_structural_meta(self):
        server = self.top.Refs._yang_fields["server"]
        self.assertEqual(server.keys, ("name",))
        self.assertEqual(server.unique, ((("port",),),))
        self.assertEqual(server.max_elements, 4)
        self.assertTrue(self.top.Refs.Server._yang_fields["proto"].mandatory)

    def test_leaf_list_min_elements(self):
        self.assertEqual(self.top.Refs._yang_fields["tags"].min_elements, 1)

    def test_leafref_target_path(self):
        # a RELATIVE leafref is enforced as an instance-scoped
        # synthesized must (RFC 7950 9.9 evaluates the path from the
        # particular leaf instance); the whole-tree schema-path check
        # is superseded, so meta.leafref stays unset
        meta = self.top.Refs._yang_fields["active_server"]
        self.assertIsNone(meta.leafref)
        self.assertIn(
            ("(../server/name) = current()",
             "leafref has no target instance with this value"),
            meta.musts,
        )

    def test_require_instance_false_opts_out(self):
        self.assertIsNone(self.top.Refs._yang_fields["unchecked_server"].leafref)

    def test_choice_membership_and_mandatory(self):
        # case is a chain of (choice, case) pairs, outermost first;
        # _yang_choices values are (mandatory, enclosing-case-chain)
        self.assertEqual(
            self.top.Refs._yang_fields["tcp_port"].case, (("transport", "tcp"),)
        )
        self.assertEqual(
            self.top.Refs._yang_fields["udp_port"].case, (("transport", "udp"),)
        )
        self.assertEqual(self.top.Refs._yang_choices, {"transport": (True, ())})

    def test_bits_meta(self):
        meta = self.top.Box._yang_fields["bits_with_default"]
        self.assertEqual(meta.encode, "bits")
        self.assertIs(meta.cls, self.bindings.BitsWithDefault)
        bit = self.bindings.BitsWithDefault._yang_fields["alpha"]
        self.assertEqual((bit.kind, bit.yang_name), ("bit", "alpha"))

    def test_metadata_invisible_to_dataclass_machinery(self):
        import dataclasses as dc

        field_names = {f.name for f in dc.fields(self.top.Refs)}
        self.assertNotIn("_yang_fields", field_names)
        # repr/eq untouched by ClassVar metadata
        self.assertEqual(self.top.Refs.Server(), self.top.Refs.Server())


class DataclassValidateTreeTests(unittest.TestCase):
    """validate_tree(): the whole-tree pass covering what on-assignment
    validation cannot (leafref integrity, mandatory, keys, unique,
    min/max-elements, choice rules, in-place list mutation)."""

    @classmethod
    def setUpClass(cls):
        cls.bindings = generate()

    def setUp(self):
        self.tree = self.bindings.Dataclass()

    def _valid_refs(self):
        Server = self.bindings.Dataclass.Refs.Server
        refs = self.tree.refs
        refs.server = [
            Server(name="a", port=80, proto="tcp"),
            Server(name="b", port=443, proto="tcp"),
        ]
        refs.active_server = "a"
        refs.tags = ["prod"]
        refs.tcp_port = 8080
        return refs

    def _violations(self):
        try:
            self.bindings.validate_tree(self.tree)
        except self.bindings.YangValidationError as exc:
            return str(exc)
        return ""

    def test_valid_tree_passes(self):
        self._valid_refs()
        self.bindings.validate_tree(self.tree)

    def test_empty_tree_enforces_top_level_mandatory(self):
        # RFC 7950: refs is a top-level non-presence container, so it
        # exists implicitly and its mandatory choice / min-elements
        # apply even to an empty tree (libyang agrees: yanglint rejects
        # {} against this schema with exactly these two violations).
        violations = self._violations()
        self.assertIn("no case of mandatory choice 'transport'", violations)
        self.assertIn("fewer than min-elements 1", violations)

    def test_leafref_violation(self):
        refs = self._valid_refs()
        refs.active_server = "nope"
        self.assertIn("no target instance", self._violations())

    def test_require_instance_false_not_checked(self):
        refs = self._valid_refs()
        refs.unchecked_server = "nope"  # same target, but require-instance false
        self.bindings.validate_tree(self.tree)

    def test_leafref_creation_order_does_not_matter(self):
        refs = self._valid_refs()
        # reference first, target afterwards -- only the final pass judges
        refs.active_server = "c"
        refs.server.append(
            self.bindings.Dataclass.Refs.Server(name="c", port=8443, proto="tcp")
        )
        self.bindings.validate_tree(self.tree)

    def test_append_hole_is_closed(self):
        refs = self._valid_refs()
        refs.tags.append(42)  # bypasses on-assignment validation
        self.assertIn("str-compatible", self._violations())

    def test_mandatory_leaf(self):
        refs = self._valid_refs()
        refs.server[0].proto = None
        self.assertIn("mandatory leaf", self._violations())

    def test_missing_and_duplicate_list_keys(self):
        refs = self._valid_refs()
        Server = self.bindings.Dataclass.Refs.Server
        refs.server.append(Server(port=1, proto="udp"))  # no key
        refs.server.append(Server(name="a", port=2, proto="udp"))  # dup key
        violations = self._violations()
        self.assertIn("list key 'name' is not set", violations)
        self.assertIn("duplicate list key", violations)

    def test_unique_violation(self):
        refs = self._valid_refs()
        refs.server[1].port = 80  # same port as server 'a'
        self.assertIn("unique", self._violations())

    def test_max_elements(self):
        refs = self._valid_refs()
        Server = self.bindings.Dataclass.Refs.Server
        for i in range(3):
            refs.server.append(Server(name="x%d" % i, port=1000 + i, proto="t"))
        self.assertIn("max-elements", self._violations())

    def test_min_elements(self):
        refs = self._valid_refs()
        refs.tags = []
        self.assertIn("min-elements", self._violations())

    def test_choice_exclusivity(self):
        refs = self._valid_refs()
        refs.udp_port = 9999  # tcp-port already set
        self.assertIn("multiple cases of choice 'transport'", self._violations())

    def test_mandatory_choice(self):
        refs = self._valid_refs()
        refs.tcp_port = None
        self.assertIn("mandatory choice 'transport'", self._violations())

    def test_violations_are_aggregated(self):
        refs = self._valid_refs()
        refs.active_server = "nope"
        refs.tags = []
        refs.server[0].proto = None
        message = self._violations()
        self.assertIn("3 violation(s)", message)

    def test_instance_paths_use_list_keys(self):
        refs = self._valid_refs()
        refs.server[1].proto = None
        self.assertIn("server[name='b']", self._violations())

    def test_fraction_digits_on_assignment(self):
        import decimal

        refs = self._valid_refs()
        refs.ratio = decimal.Decimal("1.25")
        with self.assertRaises(self.bindings.YangValidationError):
            refs.ratio = decimal.Decimal("1.256")


class DataclassOriginCommentTests(unittest.TestCase):
    """--dataclass-origin-comments: provenance comments on the line above
    nodes contributed via uses/augment; locally-defined nodes stay bare."""

    @classmethod
    def setUpClass(cls):
        cls.bindings = generate("--dataclass-origin-comments")
        cls.lines = cls.bindings._source.splitlines()

    def _comment_above(self, needle):
        for index, line in enumerate(self.lines):
            if needle in line:
                return self.lines[index - 1].strip()
        self.fail("no generated line contains %r" % needle)

    def test_uses_comment_above_field(self):
        comment = self._comment_above("host: str | None")
        self.assertRegex(comment, r"^# from dataclass\.yang:\d+, via uses endpoint at dataclass\.yang:\d+$")

    def test_augment_comment_above_field(self):
        comment = self._comment_above("augmented_note: str | None")
        self.assertRegex(comment, r"^# from dataclass\.yang:\d+, via augment at dataclass\.yang:\d+$")

    def test_local_nodes_stay_uncommented(self):
        comment = self._comment_above("plain_before: str | None")
        self.assertFalse(comment.startswith("# from"))

    def test_module_still_executes_and_validates(self):
        box = self.bindings.Dataclass.Box()
        with self.assertRaises(self.bindings.YangValidationError):
            box.number_with_default = 5

    def test_off_by_default(self):
        plain = generate()
        self.assertNotIn("# from ", plain._source)


class DataclassSerdeTests(unittest.TestCase):
    """--dataclass-serde: RFC 7951 JSON encoding over plain dicts."""

    @classmethod
    def setUpClass(cls):
        cls.bindings = generate("--dataclass-serde")

    def setUp(self):
        self.tree = self.bindings.Dataclass()

    def test_encoding_shapes(self):
        import decimal

        refs = self.tree.refs
        refs.server = [self.bindings.Dataclass.Refs.Server(name="a", port=80, proto="tcp")]
        refs.active_server = "a"
        refs.tags = ["x", "y"]
        refs.tcp_port = 8080
        refs.ratio = decimal.Decimal("1.25")
        refs.big = 2**63
        refs.blob = b"\x01\x02"
        refs.present = True
        self.tree.box.pet = "dc:dog"
        encoded = self.bindings.to_ietf_json(self.tree)

        self.assertIn("dataclass:refs", encoded)  # top level module-qualified
        refs_json = encoded["dataclass:refs"]
        self.assertEqual(
            refs_json["server"],
            [{"name": "a", "port": 80, "proto": "tcp"}],
        )
        self.assertEqual(refs_json["ratio"], "1.25")  # decimal64 -> string
        self.assertEqual(refs_json["big"], "9223372036854775808")  # uint64 -> string
        self.assertEqual(refs_json["blob"], "AQI=")  # binary -> base64
        self.assertEqual(refs_json["present"], [None])  # empty -> [null]
        # identityref canonicalised by module *name*, not prefix
        self.assertEqual(encoded["dataclass:box"]["pet"], "dataclass:dog")
        # bits -> space-joined set-bit names (alpha+gamma default to true)
        self.assertEqual(encoded["dataclass:box"]["bits-with-default"], "alpha gamma")

    def test_unset_and_empty_are_omitted(self):
        encoded = self.bindings.to_ietf_json(self.bindings.Dataclass())
        # refs is entirely unset -> omitted; box holds only YANG defaults
        self.assertNotIn("dataclass:refs", encoded)
        self.assertNotIn("plain-before", encoded.get("dataclass:box", {}))

    def test_round_trip(self):
        import decimal

        refs = self.tree.refs
        refs.server = [
            self.bindings.Dataclass.Refs.Server(name="a", port=80, proto="tcp"),
            self.bindings.Dataclass.Refs.Server(name="b", port=443, proto="udp"),
        ]
        refs.active_server = "b"
        refs.tags = ["x"]
        refs.udp_port = 9
        refs.ratio = decimal.Decimal("2.50")
        refs.big = 18446744073709551615
        refs.blob = b"hello"
        refs.present = True
        self.tree.box.pet = "dog"
        self.tree.box.mood = "happy"
        self.tree.box.bits_with_default.beta = True

        encoded = self.bindings.to_ietf_json(self.tree)
        decoded = self.bindings.from_ietf_json(
            self.bindings.Dataclass, encoded
        )
        self.assertEqual(decoded, self.tree)

    def test_decode_normalises_identityref_spelling(self):
        # the RFC 7951 spellings (module-qualified, or bare for the
        # leaf's own module) decode to the bare one; the YANG-prefix
        # spelling stays an assignment-time convenience only and is
        # REJECTED in JSON (libyang agrees)
        for spelling in ("dog", "dataclass:dog"):
            decoded = self.bindings.from_ietf_json(
                self.bindings.Dataclass, {"box": {"pet": spelling}}
            )
            self.assertEqual(decoded.box.pet, "dog", spelling)
        with self.assertRaisesRegex(ValueError, "RFC 7951"):
            self.bindings.from_ietf_json(
                self.bindings.Dataclass, {"box": {"pet": "dc:dog"}}
            )

    def test_decode_accepts_bare_names(self):
        decoded = self.bindings.from_ietf_json(
            self.bindings.Dataclass, {"box": {"plain-before": "hi"}}
        )
        self.assertEqual(decoded.box.plain_before, "hi")

    def test_decode_unknown_member_raises(self):
        with self.assertRaises(ValueError):
            self.bindings.from_ietf_json(self.bindings.Dataclass, {"nope": 1})

    def test_decode_validates_on_assignment(self):
        with self.assertRaises(self.bindings.YangValidationError):
            self.bindings.from_ietf_json(
                self.bindings.Dataclass,
                {"box": {"number-with-default": 5}},  # below range 10..4096
            )

    def test_serde_without_validation(self):
        bindings = generate("--dataclass-serde", "--no-dataclass-validation")
        self.assertFalse(hasattr(bindings, "YangValidationError"))
        tree = bindings.Dataclass()
        tree.refs.tags = ["a"]
        encoded = bindings.to_ietf_json(tree)
        self.assertEqual(encoded["dataclass:refs"]["tags"], ["a"])
        self.assertEqual(bindings.from_ietf_json(bindings.Dataclass, encoded), tree)

    def test_off_by_default(self):
        self.assertFalse(hasattr(generate(), "to_ietf_json"))


class DataclassXpathTests(unittest.TestCase):
    """--dataclass-xpaths: _yang_schema_path ClassVars + data_path()."""

    @classmethod
    def setUpClass(cls):
        cls.bindings = generate("--dataclass-xpaths")

    def test_schema_paths(self):
        self.assertEqual(self.bindings.Dataclass._yang_schema_path, "/")
        self.assertEqual(self.bindings.Dataclass.Box._yang_schema_path, "/dataclass:box")
        self.assertEqual(
            self.bindings.Dataclass.Refs.Server._yang_schema_path,
            "/dataclass:refs/server",
        )

    def test_data_path_with_key_predicates(self):
        tree = self.bindings.Dataclass()
        Server = self.bindings.Dataclass.Refs.Server
        tree.refs.server = [Server(name="a"), Server(name="b")]
        self.assertEqual(self.bindings.data_path(tree, tree), "/")
        self.assertEqual(self.bindings.data_path(tree, tree.refs), "/dataclass:refs")
        self.assertEqual(
            self.bindings.data_path(tree, tree.refs.server[1]),
            "/dataclass:refs/server[name='b']",
        )
        self.assertIsNone(self.bindings.data_path(tree, Server(name="elsewhere")))

    def test_works_without_validation(self):
        bindings = generate("--dataclass-xpaths", "--no-dataclass-validation")
        self.assertFalse(hasattr(bindings, "YangValidationError"))
        tree = bindings.Dataclass()
        self.assertEqual(bindings.data_path(tree, tree.box), "/dataclass:box")

    def test_off_by_default(self):
        plain = generate()
        self.assertFalse(hasattr(plain, "data_path"))
        self.assertFalse(hasattr(plain.Dataclass, "_yang_schema_path"))


class DataclassNoDefaultsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bindings = generate("--no-dataclass-defaults")

    def test_all_leaves_none_when_unset(self):
        box = self.bindings.Dataclass.Box()
        for name in (
            "plain_before",
            "with_default",
            "plain_after",
            "number_with_default",
            "flag_with_default",
            "from_typedef",
        ):
            self.assertIsNone(getattr(box, name), name)
        self.assertEqual(box.strings_with_defaults, [])
        # bits: dataclass of bools, all False (and thus falsy) when unset
        self.assertIs(box.bits_with_default.alpha, False)
        self.assertIs(box.bits_with_default.gamma, False)
        self.assertFalse(box.bits_with_default)

    def test_validation_still_enabled(self):
        box = self.bindings.Dataclass.Box()
        with self.assertRaises(self.bindings.YangValidationError):
            box.number_with_default = 5


class DataclassNoValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bindings = generate("--no-dataclass-validation")

    def test_no_validation_runtime(self):
        self.assertFalse(hasattr(self.bindings, "YangValidationError"))
        self.assertFalse(hasattr(self.bindings, "_YangNode"))
        # No feature needs the metadata table -> it is not emitted either.
        self.assertFalse(hasattr(self.bindings, "_FieldMeta"))
        self.assertFalse(hasattr(self.bindings.Dataclass.Box, "_yang_fields"))

    def test_assignment_never_raises(self):
        box = self.bindings.Dataclass.Box()
        box.number_with_default = 5  # out of range, but validation is off
        self.assertEqual(box.number_with_default, 5)

    def test_defaults_still_applied(self):
        self.assertEqual(self.bindings.Dataclass.Box().with_default, "lol")


class DataclassXsdPatternTests(unittest.TestCase):
    """XSD-flavored regexes (dataclass-xsd-pattern.yang): \\p{...}
    Unicode category escapes are translated to ASCII approximations
    instead of silently dropping the pattern -- dropping the BASE
    pattern of an RFC 6991-style typedef chain (ipv4-address-no-zone)
    used to leave only the weak derived restriction in force."""

    @classmethod
    def setUpClass(cls):
        cls.bindings = generate(
            yang_file=os.path.join(TEST_PATH, "dataclass-xsd-pattern.yang")
        )

    def test_patterns_accumulate_across_typedef_chain(self):
        patterns = self.bindings.DataclassXsdPattern.Box._yang_fields[
            "addr"
        ].check.patterns
        self.assertEqual(len(patterns), 2)  # derived AND (translated) base

    def test_base_pattern_enforced_through_derived_typedef(self):
        box = self.bindings.DataclassXsdPattern.Box()
        box.addr = "192.0.2.1"
        with self.assertRaises(self.bindings.YangValidationError):
            box.addr = "999.1.1.1"  # passes '[0-9\\.]*', fails the base pattern

    def test_translated_category_escape_matches(self):
        box = self.bindings.DataclassXsdPattern.Box()
        box.zoned_addr = "192.0.2.1%eth0"  # \\p{N}\\p{L} -> [0-9A-Za-z]
        with self.assertRaises(self.bindings.YangValidationError):
            box.zoned_addr = "192.0.2.1%"  # zone must be non-empty

    def test_negated_category_pattern_dropped_not_misjudged(self):
        # \\P{...} is untranslatable: the pattern is unenforced, so any
        # string is accepted (and generation did not crash).
        box = self.bindings.DataclassXsdPattern.Box()
        box.negated = "letters would violate the original pattern"

    def test_python_pattern_translation_unit(self):
        sys.path.insert(0, BASE_DIR)
        try:
            from pyangbind.plugin.pybind_dataclass import _python_pattern
        finally:
            sys.path.pop(0)
        self.assertEqual(_python_pattern("[a-z]+"), "[a-z]+")  # untouched
        # inside a character class: ASCII approximations (zone-ids)
        self.assertEqual(
            _python_pattern(r"(%[\p{N}\p{L}]+)?"), "(%[0-9A-Za-z]+)?"
        )
        # outside a class: Unicode-correct spellings where they exist
        # (libyang matches the full categories) ...
        self.assertEqual(_python_pattern(r"\p{L}+"), r"[^\W\d_]+")
        self.assertEqual(_python_pattern(r"\p{Nd}*"), r"\d*")
        # ... and inexpressible categories drop the pattern instead of
        # narrowing it to ASCII (unenforced rather than misjudged)
        self.assertIsNone(_python_pattern(r"\p{Lu}\p{Nd}*"))
        self.assertIsNone(_python_pattern(r"[\P{L}]+"))  # no safe negation
        self.assertIsNone(_python_pattern(r"\p{Zs}"))  # unmapped category


class DataclassNativeInetTests(unittest.TestCase):
    """Native IP types (on by default): ietf-inet-types address/prefix
    typedefs map onto the stdlib ipaddress classes
    (dataclass-inet.yang)."""

    @classmethod
    def setUpClass(cls):
        cls.yang = os.path.join(TEST_PATH, "dataclass-inet.yang")
        cls.bindings = generate("--dataclass-serde", yang_file=cls.yang)

    def setUp(self):
        import ipaddress

        self.ip = ipaddress
        self.net = self.bindings.DataclassInet().net

    def test_annotations(self):
        anns = self.bindings.DataclassInet.Net.__annotations__
        self.assertEqual(anns["v4"], "ipaddress.IPv4Address | None")
        self.assertEqual(anns["v6"], "ipaddress.IPv6Address | None")
        self.assertEqual(
            anns["any_address"],
            "ipaddress.IPv4Address | ipaddress.IPv6Address | None",
        )
        self.assertEqual(
            anns["servers"], "list[ipaddress.IPv4Address | ipaddress.IPv6Address]"
        )
        self.assertEqual(anns["prefix4"], "ipaddress.IPv4Network | None")
        self.assertEqual(
            anns["any_prefix"],
            "ipaddress.IPv4Network | ipaddress.IPv6Network | None",
        )
        # derived typedef resolves through to the inet base
        self.assertIn("IPv4Address", anns["gateway"])
        # union member: native alongside the other members
        self.assertEqual(anns["addr_or_name"], "ipaddress.IPv4Address | str | None")
        # non-address inet typedefs stay untouched
        self.assertEqual(anns["port"], "int | None")

    def test_class_checked_instead_of_pattern(self):
        self.net.v4 = self.ip.IPv4Address("10.0.0.1")
        with self.assertRaises(self.bindings.YangValidationError):
            self.net.v4 = "10.0.0.1"  # the string spelling is no longer a value
        with self.assertRaises(self.bindings.YangValidationError):
            self.net.prefix4 = self.ip.IPv6Network("2001:db8::/32")  # wrong family
        self.net.any_address = self.ip.IPv6Address("2001:db8::1")  # either family

    def test_ipv6_zone_rides_scope_id_and_no_zone_rejects_it(self):
        self.net.v6 = self.ip.IPv6Address("fe80::1%eth0")
        with self.assertRaises(self.bindings.YangValidationError):
            self.net.v6_no_zone = self.ip.IPv6Address("fe80::1%eth0")
        self.net.v6_no_zone = self.ip.IPv6Address("fe80::1")

    def test_typedef_default_constructs_the_object(self):
        self.assertEqual(self.net.gateway, self.ip.IPv4Address("192.0.2.1"))

    def test_union_keeps_other_members(self):
        self.net.addr_or_name = self.ip.IPv4Address("192.0.2.9")
        self.net.addr_or_name = "gateway"
        with self.assertRaises(self.bindings.YangValidationError):
            self.net.addr_or_name = "NOPE"  # fails the string member's pattern

    def test_serde_round_trip(self):
        tree = self.bindings.DataclassInet()
        net = tree.net
        net.v6 = self.ip.IPv6Address("fe80::1%eth0")
        net.servers = [
            self.ip.IPv4Address("192.0.2.7"),
            self.ip.IPv6Address("2001:db8::7"),
        ]
        net.prefix4 = self.ip.IPv4Network("10.0.0.0/24")
        net.any_prefix = self.ip.IPv6Network("2001:db8::/64")
        net.addr_or_name = self.ip.IPv4Address("192.0.2.9")
        encoded = self.bindings.to_ietf_json(tree)
        net_json = encoded["dataclass-inet:net"]
        self.assertEqual(net_json["v6"], "fe80::1%eth0")  # zone survives str()
        self.assertEqual(net_json["prefix4"], "10.0.0.0/24")
        self.assertEqual(net_json["addr-or-name"], "192.0.2.9")  # union member
        decoded = self.bindings.from_ietf_json(self.bindings.DataclassInet, encoded)
        self.assertEqual(decoded, tree)
        # the mixed union's *string* member round-trips as a string
        net.addr_or_name = "gateway"
        decoded = self.bindings.from_ietf_json(
            self.bindings.DataclassInet, self.bindings.to_ietf_json(tree)
        )
        self.assertEqual(decoded.net.addr_or_name, "gateway")

    def test_serde_without_validation_still_decodes_natives(self):
        bindings = generate(
            "--dataclass-serde", "--no-dataclass-validation", yang_file=self.yang
        )
        tree = bindings.DataclassInet()
        tree.net.v4 = self.ip.IPv4Address("10.0.0.1")
        tree.net.addr_or_name = self.ip.IPv4Address("192.0.2.9")
        decoded = bindings.from_ietf_json(
            bindings.DataclassInet, bindings.to_ietf_json(tree)
        )
        self.assertEqual(decoded, tree)

    def test_opt_out_flag(self):
        bindings = generate("--no-dataclass-native-ip-types", yang_file=self.yang)
        anns = bindings.DataclassInet.Net.__annotations__
        self.assertEqual(anns["v4"], "str | None")
        self.assertEqual(anns["any_prefix"], "str | None")
        net = bindings.DataclassInet().net
        net.v4 = "10.0.0.1"  # pattern-checked string, as before
        with self.assertRaises(bindings.YangValidationError):
            net.v4 = "999.0.0.1"
        net.v4_zoned = "10.0.0.1%3"  # IPv4 zone index expressible as a string
        self.assertEqual(net.gateway, "192.0.2.1")


class DataclassNativeHintTests(unittest.TestCase):
    """The --dataclass-native-type option (dataclass-native-hint.yang):
    generator-supplied native Python classes for schema-defined string
    typedefs -- a generator-only concern, never YANG metadata. E.g.
    ipaddress.IPv4Interface for ADDR/PREFIXLEN values, the one
    interface-address shape RFC 6991 has no typedef for."""

    HINT_FLAGS = (
        # module-qualified spelling
        "--dataclass-native-type",
        "dataclass-native-hint:ipv4-address-and-prefix=ipaddress.IPv4Interface",
        # unqualified (any-module) spelling, multi-class union
        "--dataclass-native-type",
        "ip-address-and-prefix=ipaddress.IPv4Interface,ipaddress.IPv6Interface",
        # a non-ipaddress package, pinning the generic import emission
        "--dataclass-native-type",
        "posix-path=pathlib.PurePosixPath",
    )

    @classmethod
    def setUpClass(cls):
        cls.yang = os.path.join(TEST_PATH, "dataclass-native-hint.yang")
        cls.bindings = generate(
            "--dataclass-serde", *cls.HINT_FLAGS, yang_file=cls.yang
        )

    def setUp(self):
        import ipaddress

        self.ip = ipaddress
        self.host = self.bindings.DataclassNativeHint().host

    def test_annotations(self):
        anns = self.bindings.DataclassNativeHint.Host.__annotations__
        self.assertEqual(anns["addr4"], "ipaddress.IPv4Interface | None")
        self.assertEqual(
            anns["addr"], "ipaddress.IPv4Interface | ipaddress.IPv6Interface | None"
        )
        self.assertEqual(anns["workdir"], "pathlib.PurePosixPath | None")
        # union: the hinted member joins the inet-native and str members
        self.assertEqual(
            anns["addr_or_bare_or_name"],
            "ipaddress.IPv4Interface | ipaddress.IPv6Interface | "
            "ipaddress.IPv4Address | ipaddress.IPv6Address | str | None",
        )

    def test_isinstance_replaces_the_string_pattern(self):
        self.host.addr4 = self.ip.IPv4Interface("10.0.12.3/24")  # host bits kept
        with self.assertRaises(self.bindings.YangValidationError):
            self.host.addr4 = "10.0.12.3/24"  # the string spelling
        with self.assertRaises(self.bindings.YangValidationError):
            self.host.addr4 = self.ip.IPv6Interface("2001:db8::1/64")  # v4-only hint

    def test_default_constructs_the_class(self):
        self.assertEqual(
            self.host.addr_with_default, self.ip.IPv4Interface("192.0.2.1/24")
        )

    def test_non_ipaddress_package_hint_and_import(self):
        import pathlib

        self.host.workdir = pathlib.PurePosixPath("/etc/network")
        with self.assertRaises(self.bindings.YangValidationError):
            self.host.workdir = "/etc/network"

    def test_unmapped_typedefs_stay_strings(self):
        bindings = generate(yang_file=self.yang)  # no --dataclass-native-type
        anns = bindings.DataclassNativeHint.Host.__annotations__
        self.assertEqual(anns["addr4"], "str | None")
        self.assertEqual(anns["workdir"], "str | None")

    def test_union_members_keep_their_types_through_serde(self):
        tree = self.bindings.DataclassNativeHint()
        for value in (
            self.ip.IPv4Address("10.0.0.1"),  # bare: never gains a /32
            self.ip.IPv4Interface("10.0.12.3/24"),  # host bits survive
            self.ip.IPv6Interface("2001:db8::1/64"),
            "gateway",  # the plain-string member
        ):
            tree.host.addr_or_bare_or_name = value
            decoded = self.bindings.from_ietf_json(
                self.bindings.DataclassNativeHint, self.bindings.to_ietf_json(tree)
            )
            self.assertEqual(decoded, tree)
            self.assertIs(type(decoded.host.addr_or_bare_or_name), type(value))

    def test_leaf_list_round_trip(self):
        tree = self.bindings.DataclassNativeHint()
        tree.host.addrs = [
            self.ip.IPv4Interface("10.0.0.1/8"),
            self.ip.IPv6Interface("2001:db8::7/64"),
        ]
        self.bindings.validate_tree(tree)
        encoded = self.bindings.to_ietf_json(tree)
        self.assertEqual(
            encoded["dataclass-native-hint:host"]["addrs"],
            ["10.0.0.1/8", "2001:db8::7/64"],
        )
        decoded = self.bindings.from_ietf_json(
            self.bindings.DataclassNativeHint, encoded
        )
        self.assertEqual(decoded, tree)

    def test_hint_wins_over_no_native_ip_types_flag(self):
        # the flag disables the built-in ietf mapping only; an explicit
        # --dataclass-native-type mapping stays honored
        bindings = generate(
            "--no-dataclass-native-ip-types", *self.HINT_FLAGS, yang_file=self.yang
        )
        anns = bindings.DataclassNativeHint.Host.__annotations__
        self.assertEqual(anns["addr4"], "ipaddress.IPv4Interface | None")
        self.assertIn("str", anns["addr_or_bare_or_name"])  # inet member reverts


class DataclassMustWhenTests(unittest.TestCase):
    """validate_tree() evaluates YANG must/when constraints with the
    embedded XPath 1.0 subset engine (dataclass-constraints.yang)."""

    @classmethod
    def setUpClass(cls):
        cls.yang = os.path.join(TEST_PATH, "dataclass-constraints.yang")
        cls.bindings = generate(yang_file=cls.yang)

    def setUp(self):
        self.tree = self.bindings.DataclassConstraints()
        self.Vrf = self.bindings.DataclassConstraints.Net.Vrf

    def _violations(self):
        try:
            self.bindings.validate_tree(self.tree)
        except self.bindings.YangValidationError as exc:
            return str(exc)
        return ""

    def test_must_absolute_path_with_current(self):
        self.tree.net.vrf = [
            self.Vrf(name="u", role="underlay"),
            self.Vrf(name="t", role="tenant", underlay_ref="u"),
        ]
        self.bindings.validate_tree(self.tree)
        self.tree.net.vrf[1].underlay_ref = "t"  # tenant, not underlay
        self.assertIn("underlay-ref must point at an underlay vrf", self._violations())

    def test_must_relative_comparison(self):
        self.tree.net.vrf = [self.Vrf(name="a", mtu=1500)]
        self.bindings.validate_tree(self.tree)
        self.tree.net.vrf[0].mtu = 100
        self.assertIn("violates must", self._violations())

    def test_must_count(self):
        self.tree.net.members.member = ["x", "y"]
        self.bindings.validate_tree(self.tree)
        self.tree.net.members.member = ["x", "y", "z"]
        self.assertIn("count(", self._violations())

    def test_when_direct(self):
        self.tree.net.vrf = [self.Vrf(name="a", enabled=True, detail="d")]
        self.bindings.validate_tree(self.tree)
        self.tree.net.vrf[0].enabled = False
        self.assertIn("when condition", self._violations())

    def test_when_absent_node_never_violates(self):
        self.tree.net.vrf = [self.Vrf(name="a")]  # detail unset, when false
        self.bindings.validate_tree(self.tree)

    def test_when_on_choice_applies_to_flattened_fields(self):
        self.tree.net.vrf = [self.Vrf(name="a", enabled=True, v4="10.0.0.1")]
        self.bindings.validate_tree(self.tree)
        self.tree.net.vrf[0].enabled = False
        self.assertIn("when condition", self._violations())

    def test_when_on_uses_has_parent_context(self):
        self.tree.wrap.mode = "a"
        self.tree.wrap.extra = "x"
        self.bindings.validate_tree(self.tree)
        self.tree.wrap.mode = "b"
        self.assertIn("wrap/extra", self._violations())

    def test_when_on_augment_has_parent_context(self):
        self.tree.wrap.mode = "b"
        self.tree.wrap.b_only = "x"
        self.bindings.validate_tree(self.tree)
        self.tree.wrap.mode = "a"
        self.assertIn("wrap/b-only", self._violations())

    def test_re_match(self):
        # re-match() is anchored and XSD-flavored
        self.tree.net.vrf = [self.Vrf(name="a", odd="aaa")]
        self.bindings.validate_tree(self.tree)
        self.tree.net.vrf = [self.Vrf(name="a", odd="zzz")]
        self.assertIn("re-match", self._violations())

    def test_unsupported_expression_is_skipped_not_misjudged(self):
        # deref() is not implemented; the must on `linked` is filtered
        # out at codegen, so any value passes
        self.tree.net.vrf = [self.Vrf(name="a", linked="anything")]
        self.bindings.validate_tree(self.tree)
        self.assertNotIn("deref", self.bindings._source)

    def test_opt_out_flag(self):
        bindings = generate("--no-dataclass-must-when", yang_file=self.yang)
        self.assertNotIn("musts=", bindings._source)
        self.assertNotIn("whens=", bindings._source)
        tree = bindings.DataclassConstraints()
        Vrf = bindings.DataclassConstraints.Net.Vrf
        tree.net.vrf = [Vrf(name="a", mtu=100)]  # violates the must
        bindings.validate_tree(tree)  # ... which is not emitted


class DataclassDumbTests(unittest.TestCase):
    """The feature-free pybind-dataclass-dumb variant shares the
    structural bits-as-dataclass-of-bools shape, without behavior."""

    @classmethod
    def setUpClass(cls):
        cls.bindings = generate(output_format="pybind-dataclass-dumb")

    def test_bits_dataclass_of_bools(self):
        box = self.bindings.Dataclass.Box()
        self.assertIs(box.bits_with_default.alpha, False)  # no YANG defaults applied
        self.assertFalse(box.bits_with_default)
        box.bits_with_default.beta = True
        self.assertTrue(box.bits_with_default)

    def test_no_features(self):
        self.assertFalse(hasattr(self.bindings, "YangValidationError"))
        box = self.bindings.Dataclass.Box()
        self.assertIsNone(box.with_default)
        box.number_with_default = 5  # no validation


class DataclassAllOffTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bindings = generate("--no-dataclass-validation", "--no-dataclass-defaults")

    def test_bare_bindings(self):
        box = self.bindings.Dataclass.Box()
        self.assertIsNone(box.with_default)
        box.number_with_default = 5  # no validation
        self.assertFalse(hasattr(self.bindings, "YangValidationError"))


class DataclassSplitDirTests(unittest.TestCase):
    """--dataclass-split-dir: package output, shared code emitted once."""

    @classmethod
    def setUpClass(cls):
        import importlib
        import tempfile

        cls.tmp = tempfile.mkdtemp()
        cls.pkg_dir = os.path.join(cls.tmp, "dc_split")
        pyang = shutil.which("pyang")
        if pyang is None:
            raise RuntimeError("Could not locate `pyang` executable.")
        subprocess.check_output(
            [
                pyang,
                "--plugindir",
                PLUGIN_DIR,
                "-f",
                "pybind-dataclass",
                "-p",
                TEST_PATH,
                "--dataclass-serde",
                "--dataclass-xpaths",
                "--dataclass-split-dir",
                cls.pkg_dir,
                os.path.join(TEST_PATH, "dataclass.yang"),
            ],
            stderr=subprocess.PIPE,
            env={"PYTHONPATH": BASE_DIR},
        )
        sys.path.insert(0, cls.tmp)
        cls.bindings = importlib.import_module("dc_split")

    @classmethod
    def tearDownClass(cls):
        sys.path.remove(cls.tmp)
        for name in [n for n in sys.modules if n.split(".")[0] == "dc_split"]:
            del sys.modules[name]
        shutil.rmtree(cls.tmp)

    def _sources(self):
        for filename in sorted(os.listdir(self.pkg_dir)):
            if filename.endswith(".py"):
                with open(os.path.join(self.pkg_dir, filename)) as fd:
                    yield filename, fd.read()

    def test_package_layout(self):
        self.assertEqual(
            sorted(f for f, _ in self._sources()),
            ["__init__.py", "_runtime.py", "_types.py", "dataclass.py"],
        )

    def test_shared_code_emitted_once(self):
        for marker in (
            "class YangValidationError",
            "class _FieldMeta",
            "def validate_tree",
            "def to_ietf_json",
            "def data_path",
            "type Animal =",
        ):
            hits = [f for f, src in self._sources() if marker in src]
            self.assertEqual(len(hits), 1, "%r found in %s" % (marker, hits))

    def test_same_import_surface_as_single_file(self):
        # runtime API, reusable aliases and the tree class all resolve on
        # the package itself, like they do on a single-file module
        for name in (
            "Dataclass",
            "Animal",
            "YangValidationError",
            "validate_tree",
            "to_ietf_json",
            "from_ietf_json",
            "data_path",
        ):
            self.assertTrue(hasattr(self.bindings, name), name)

    def test_bindings_work_end_to_end(self):
        tree = self.bindings.Dataclass()
        tree.box.pet = "dc:dog"
        with self.assertRaises(self.bindings.YangValidationError):
            tree.box.mood = "angry"
        # refs is a top-level non-presence container: its mandatory
        # choice and min-elements apply implicitly, so satisfy them.
        tree.refs.tags = ["prod"]
        tree.refs.tcp_port = 8080
        self.bindings.validate_tree(tree)
        encoded = self.bindings.to_ietf_json(tree)
        self.assertEqual(encoded["dataclass:box"]["pet"], "dataclass:dog")
        decoded = self.bindings.from_ietf_json(self.bindings.Dataclass, encoded)
        self.assertEqual(decoded.box.pet, "dog")  # normalised spelling
        self.assertEqual(
            self.bindings.data_path(tree, tree.box), "/dataclass:box"
        )


class DataclassMandatoryPropagationTests(unittest.TestCase):
    """RFC 7950 mandatory-node propagation: a non-presence container
    with a mandatory descendant is itself mandatory, so its checks
    apply even when it holds no data, wherever its context exists.
    Every verdict here matches yanglint (libyang) on the same data."""

    @classmethod
    def setUpClass(cls):
        cls.bindings = generate(
            yang_file=os.path.join(TEST_PATH, "dataclass-mandatory.yang")
        )

    def setUp(self):
        self.tree = self.bindings.DataclassMandatory()
        self.entry = self.bindings.DataclassMandatory.Item(name="a")
        self.tree.item.append(self.entry)

    def _violations(self):
        try:
            self.bindings.validate_tree(self.tree)
        except self.bindings.YangValidationError as exc:
            return str(exc)
        return ""

    def test_empty_entry_all_implicit_violations(self):
        violations = self._violations()
        # libyang reports exactly these five on the same instance data
        self.assertIn("np-box/rt: mandatory leaf is not set", violations)
        self.assertIn("np-chain/inner/deep: mandatory leaf is not set", violations)
        self.assertIn("no case of mandatory choice 'encoding'", violations)
        self.assertIn("fewer than min-elements 1", violations)
        self.assertIn("np-must needs x or y", violations)
        self.assertEqual(violations.count("\n"), 5)  # header + 5 lines

    def test_absent_presence_container_not_enforced(self):
        violations = self._violations()
        self.assertNotIn("/p-box", violations)

    def test_unselected_case_not_enforced(self):
        violations = self._violations()
        self.assertNotIn("in-case", violations)

    def test_when_guarded_container_skipped_when_empty(self):
        violations = self._violations()
        self.assertNotIn("guarded", violations)

    def test_selected_case_enforces_its_container(self):
        # touching in-case selects case 'a', so its mandatory leaf and
        # the implicit checks hoisted to it now apply
        self.entry.in_case.req = None  # no data: case still unselected
        self.assertNotIn("in-case", self._violations())
        self.entry.in_case.req = "set"
        self.assertNotIn("in-case", self._violations())

    def test_satisfied_entry_passes(self):
        entry = self.entry
        entry.np_box.rt = "x"
        entry.np_chain.inner.deep = "d"
        entry.np_choice_box.plain = "p"
        entry.np_min.tags = ["t"]
        entry.other = "b-case"
        entry.np_must.x = "1"
        self.bindings.validate_tree(self.tree)  # must not raise

    def test_presence_container_with_data_enforces_mandatory(self):
        entry = self.entry
        entry.np_box.rt = "x"
        entry.np_chain.inner.deep = "d"
        entry.np_choice_box.plain = "p"
        entry.np_min.tags = ["t"]
        entry.np_must.x = "1"
        entry.p_box.rt = "y"  # presence container now exists and is valid
        self.bindings.validate_tree(self.tree)


class DataclassConformanceTests(unittest.TestCase):
    """Conformance details pinned by differential testing against
    libyang (yanglint agrees with every verdict here)."""

    @classmethod
    def setUpClass(cls):
        cls.bindings = generate(
            "--dataclass-serde",
            yang_file=os.path.join(TEST_PATH, "dataclass-conformance.yang"),
        )

    def setUp(self):
        self.tree = self.bindings.DataclassConformance()

    def _violations(self):
        try:
            self.bindings.validate_tree(self.tree)
        except self.bindings.YangValidationError as exc:
            return str(exc)
        return ""

    def test_invert_match_pattern(self):
        self.tree.inverted = "yes"  # does not match x.* -> valid
        with self.assertRaisesRegex(
            self.bindings.YangValidationError, "invert-match"
        ):
            self.tree.inverted = "xyz"

    def test_nested_mandatory_choice_gated_by_outer_case(self):
        # outer case not selected: inner's mandatory does not apply
        self.tree.c.ob = "b-case"
        self.assertEqual(self._violations(), "")
        # selecting outer case 'a' without an inner case is a violation
        tree2 = self.bindings.DataclassConformance()
        tree2.c.marker = "m"
        self.tree = tree2
        self.assertIn("mandatory choice 'inner'", self._violations())
        # and satisfying inner clears it
        tree2.c.ia = "x"
        self.assertEqual(self._violations(), "")

    def test_nested_choice_field_selects_whole_chain(self):
        # ia (case ia of inner, inside case a of outer) conflicts with
        # ob (case b of outer): outer exclusivity is still caught
        self.tree.c.ia = "x"
        self.tree.c.ob = "y"
        self.assertIn("multiple cases of choice 'outer'", self._violations())

    def test_int64_decode_requires_string(self):
        decoded = self.bindings.from_ietf_json(
            self.bindings.DataclassConformance, {"dataclass-conformance:big": "5"}
        )
        self.assertEqual(decoded.big, 5)
        with self.assertRaisesRegex(ValueError, "RFC 7951"):
            self.bindings.from_ietf_json(
                self.bindings.DataclassConformance, {"dataclass-conformance:big": 5}
            )

    def test_empty_decode_requires_null_array(self):
        decoded = self.bindings.from_ietf_json(
            self.bindings.DataclassConformance, {"dataclass-conformance:e": [None]}
        )
        self.assertIs(decoded.e, True)
        with self.assertRaisesRegex(ValueError, "RFC 7951"):
            self.bindings.from_ietf_json(
                self.bindings.DataclassConformance, {"dataclass-conformance:e": True}
            )

    def test_bits_decode_rejects_unknown_names(self):
        decoded = self.bindings.from_ietf_json(
            self.bindings.DataclassConformance,
            {"dataclass-conformance:flags": "b1 b2"},
        )
        self.assertTrue(decoded.flags.b1 and decoded.flags.b2)
        with self.assertRaisesRegex(ValueError, "unknown bit"):
            self.bindings.from_ietf_json(
                self.bindings.DataclassConformance,
                {"dataclass-conformance:flags": "b1 b9"},
            )

    def test_unicode_category_pattern_outside_class(self):
        self.tree.letters = "äö"  # \p{L} matches all Unicode letters
        with self.assertRaises(self.bindings.YangValidationError):
            self.tree.letters = "a1"

    def test_xsd_anchors_are_literal(self):
        self.tree.anchored = "a$b"  # $ is an ordinary character in XSD
        with self.assertRaises(self.bindings.YangValidationError):
            self.tree.anchored = "ab"

    def test_binary_default_is_bytes(self):
        # generated with defaults here (no --no-dataclass-defaults)
        self.assertEqual(self.tree.blob, b"yang")
        self.bindings.validate_tree(self.tree)

    def test_instance_identifier_syntax(self):
        self.tree.target = "/dcc:srv[name='a'][role='x']/dcc:name"
        with self.assertRaisesRegex(
            self.bindings.YangValidationError, "instance-identifier"
        ):
            self.tree.target = "not a path"

    def test_predicated_leafref(self):
        Srv = self.bindings.DataclassConformance.Srv
        Use = self.bindings.DataclassConformance.Use
        self.tree.srv = [Srv(name="a", role="db")]
        self.tree.use = [Use(id="u", want_role="db", pick="a")]
        self.assertEqual(self._violations(), "")
        self.tree.srv[0].role = "web"  # predicate no longer selects srv 'a'
        self.assertIn("leafref has no target instance", self._violations())

    def test_union_string_member_decoding(self):
        decoded = self.bindings.from_ietf_json(
            self.bindings.DataclassConformance, {"dataclass-conformance:u": "3.5"}
        )
        import decimal as _decimal

        self.assertEqual(decoded.u, _decimal.Decimal("3.5"))
        decoded = self.bindings.from_ietf_json(
            self.bindings.DataclassConformance, {"dataclass-conformance:u": "off"}
        )
        self.assertEqual(decoded.u, "off")
        decoded = self.bindings.from_ietf_json(
            self.bindings.DataclassConformance, {"dataclass-conformance:ub": "x y"}
        )
        self.assertEqual(decoded.ub, frozenset({"x", "y"}))
        # and the bits set round-trips back to the RFC 7951 string form
        self.assertEqual(
            self.bindings.to_ietf_json(decoded)["dataclass-conformance:ub"], "x y"
        )
        with self.assertRaises(ValueError):
            self.bindings.from_ietf_json(
                self.bindings.DataclassConformance,
                {"dataclass-conformance:u": "9.5"},
            )

    def test_empty_presence_container_decode_rejected(self):
        with self.assertRaisesRegex(ValueError, "not\\s+representable"):
            self.bindings.from_ietf_json(
                self.bindings.DataclassConformance,
                {"dataclass-conformance:pbox": {}},
            )
        decoded = self.bindings.from_ietf_json(
            self.bindings.DataclassConformance,
            {"dataclass-conformance:pbox": {"setting": "s"}},
        )
        self.assertEqual(decoded.pbox.setting, "s")


if __name__ == "__main__":
    unittest.main()

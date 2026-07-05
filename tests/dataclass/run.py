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
        # both bare and module-prefixed spellings of every derived identity
        self.assertEqual(values, {"dog", "cat", "dc:dog", "dc:cat"})
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
        self.assertEqual(server.unique, (("port",),))
        self.assertEqual(server.max_elements, 4)
        self.assertTrue(self.top.Refs.Server._yang_fields["proto"].mandatory)

    def test_leaf_list_min_elements(self):
        self.assertEqual(self.top.Refs._yang_fields["tags"].min_elements, 1)

    def test_leafref_target_path(self):
        meta = self.top.Refs._yang_fields["active_server"]
        self.assertEqual(meta.leafref, "/dataclass:refs/server/name")

    def test_require_instance_false_opts_out(self):
        self.assertIsNone(self.top.Refs._yang_fields["unchecked_server"].leafref)

    def test_choice_membership_and_mandatory(self):
        self.assertEqual(
            self.top.Refs._yang_fields["tcp_port"].case, ("transport", "tcp")
        )
        self.assertEqual(
            self.top.Refs._yang_fields["udp_port"].case, ("transport", "udp")
        )
        self.assertEqual(self.top.Refs._yang_choices, {"transport": True})

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

    def test_empty_tree_passes(self):
        # refs never comes into existence, so its mandatory choice,
        # mandatory leaves and min-elements do not apply.
        self.bindings.validate_tree(self.tree)

    def test_leafref_violation(self):
        refs = self._valid_refs()
        refs.active_server = "nope"
        self.assertIn("no matching instance", self._violations())

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


if __name__ == "__main__":
    unittest.main()

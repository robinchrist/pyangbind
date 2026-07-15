"""Differential test corpus, batch 5: former known-divergence fixes."""
from cases import M


CASES = [
    dict(
        name="leafref-instance-scope",
        yang=M("dt-lscope", """
  list box {
    key "name";
    leaf name { type string; }
    list srv { key "sn"; leaf sn { type string; } }
    leaf pick { type leafref { path "../srv/sn"; } }
    leaf-list picks { type leafref { path "../srv/sn"; } }
  }
"""),
        docs=[
            ("same-entry-ok", {"dt-lscope:box": [
                {"name": "b1", "srv": [{"sn": "s1"}], "pick": "s1"}]}),
            ("cross-entry-dangling", {"dt-lscope:box": [
                {"name": "b1", "srv": [{"sn": "only-b1"}]},
                {"name": "b2", "pick": "only-b1"}]}),
            ("leaflist-cross-entry", {"dt-lscope:box": [
                {"name": "b1", "srv": [{"sn": "only-b1"}]},
                {"name": "b2", "srv": [{"sn": "own"}], "picks": ["own", "only-b1"]}]}),
        ],
    ),
    dict(
        name="unique-descendant",
        yang=M("dt-uniqd", """
  list l {
    key "k";
    unique "c/v name";
    leaf k { type string; }
    leaf name { type string; }
    container c { leaf v { type uint8; } }
  }
"""),
        docs=[
            ("dup-pair", {"dt-uniqd:l": [
                {"k": "a", "name": "n", "c": {"v": 1}},
                {"k": "b", "name": "n", "c": {"v": 1}}]}),
            ("distinct-descendant", {"dt-uniqd:l": [
                {"k": "a", "name": "n", "c": {"v": 1}},
                {"k": "b", "name": "n", "c": {"v": 2}}]}),
            ("absent-leaf-skips", {"dt-uniqd:l": [
                {"k": "a", "name": "n"},
                {"k": "b", "name": "n", "c": {"v": 1}}]}),
        ],
    ),
    dict(
        name="derived-from-transitive",
        yang=M("dt-dft", """
  identity base;
  identity mid { base base; }
  identity deep { base mid; }
  identity other { base base; }
  leaf kind { type identityref { base base; } }
  leaf need-mid { type string; must "derived-from(../kind, 'mid')"; }
  leaf need-mid-self { type string; must "derived-from-or-self(../kind, 'mid')"; }
"""),
        docs=[
            ("transitive-true", {"dt-dft:kind": "deep", "dt-dft:need-mid": "x"}),
            ("self-not-derived", {"dt-dft:kind": "mid", "dt-dft:need-mid": "x"}),
            ("unrelated", {"dt-dft:kind": "other", "dt-dft:need-mid": "x"}),
            ("or-self-true", {"dt-dft:kind": "mid", "dt-dft:need-mid-self": "x"}),
            ("or-self-transitive", {"dt-dft:kind": "deep", "dt-dft:need-mid-self": "x"}),
            ("or-self-unrelated", {"dt-dft:kind": "other", "dt-dft:need-mid-self": "x"}),
        ],
    ),
    dict(
        name="when-guarded-mandatory",
        yang=M("dt-wgm", """
  leaf mode { type string; }
  container g {
    when "../mode = 'on'";
    leaf req { type string; mandatory true; }
  }
  container outer {
    when "../mode = 'deep'";
    container inner { leaf need { type string; mandatory true; } }
  }
  leaf other { type string; }
"""),
        docs=[
            ("when-true-empty", {"dt-wgm:mode": "on", "dt-wgm:other": "x"}),
            ("when-false-empty", {"dt-wgm:mode": "off", "dt-wgm:other": "x"}),
            ("when-true-satisfied", {"dt-wgm:mode": "on", "dt-wgm:g": {"req": "r"},
                                     "dt-wgm:other": "x"}),
            ("nested-when-true-empty", {"dt-wgm:mode": "deep", "dt-wgm:other": "x"}),
        ],
    ),
    dict(
        name="unicode-categories",
        yang=M("dt-ucat", """
  leaf not-letters { type string { pattern '[\\P{L}]+'; } }
  leaf upper { type string { pattern '\\p{Lu}+'; } }
  leaf spaces { type string { pattern '\\p{Zs}' ; } }
  leaf zone { type string { pattern '(%[\\p{N}\\p{L}]+)?'; } }
"""),
        docs=[
            ("notletters-ok", {"dt-ucat:not-letters": "123 .-"}),
            ("notletters-violation", {"dt-ucat:not-letters": "ab"}),
            ("upper-unicode-ok", {"dt-ucat:upper": "AÄÖ"}),
            ("upper-lower-bad", {"dt-ucat:upper": "Aa"}),
            ("space-ok", {"dt-ucat:spaces": " "}),
            ("space-bad", {"dt-ucat:spaces": "x"}),
            ("zone-non-ascii-ok", {"dt-ucat:zone": "%λ1"}),
            ("zone-bad", {"dt-ucat:zone": "%!"}),
        ],
    ),
    dict(
        name="xpath-functions-2",
        yang=M("dt-xf2", """
  container c {
    list item { key "k"; leaf k { type string; } leaf v { type uint8; } }
    leaf closing {
      type string;
      must "../item[last()]/v = 9";
    }
    leaf head {
      type string;
      must "../item[position() = 1]/v = 1";
    }
    leaf pre { type string; must "substring-before(., '-') = 'ab'"; }
    leaf post { type string; must "substring-after(., '-') = 'cd'"; }
    leaf mid { type string; must "substring(., 2, 2) = 'xy'"; }
    leaf tr { type string; must "translate(., 'abc', 'xyz') = 'xyz'"; }
    leaf ns { type string; must "normalize-space(.) = 'a b'"; }
    leaf fl { type uint8; must "floor(. div 2) = 2"; }
    leaf ce { type uint8; must "ceiling(. div 2) = 3"; }
    leaf ro { type uint8; must "round(. div 4) = 1"; }
    leaf total { type uint8; must ". = sum(../item/v)"; }
    leaf flags { type bits { bit b1; bit b2; } }
    leaf need-b1 { type string; must "bit-is-set(../flags, 'b1')"; }
    leaf color { type enumeration { enum red { value 5; } enum blue; } }
    leaf ev { type string; must "enum-value(../color) = 6"; }
    leaf pick { type leafref { path "../item/k"; } }
    leaf via { type string; must "deref(../pick)/../v = 7"; }
  }
"""),
        docs=[
            ("positional-ok", {"dt-xf2:c": {"item": [{"k": "a", "v": 1}, {"k": "b", "v": 9}],
                                            "closing": "x", "head": "x"}}),
            ("positional-bad", {"dt-xf2:c": {"item": [{"k": "a", "v": 1}, {"k": "b", "v": 2}],
                                             "closing": "x"}}),
            ("strings-ok", {"dt-xf2:c": {"pre": "ab-zz", "post": "zz-cd", "mid": "zxyz",
                                         "tr": "abc", "ns": "  a   b  "}}),
            ("strings-bad", {"dt-xf2:c": {"pre": "zz-ab"}}),
            ("math-ok", {"dt-xf2:c": {"fl": 5, "ce": 5, "ro": 5}}),
            ("math-bad", {"dt-xf2:c": {"fl": 7}}),
            ("sum-ok", {"dt-xf2:c": {"item": [{"k": "a", "v": 1}, {"k": "b", "v": 9}],
                                     "total": 10}}),
            ("sum-bad", {"dt-xf2:c": {"item": [{"k": "a", "v": 1}], "total": 3}}),
            ("bits-ok", {"dt-xf2:c": {"flags": "b1 b2", "need-b1": "x"}}),
            ("bits-bad", {"dt-xf2:c": {"flags": "b2", "need-b1": "x"}}),
            ("enum-ok", {"dt-xf2:c": {"color": "blue", "ev": "x"}}),
            ("enum-bad", {"dt-xf2:c": {"color": "red", "ev": "x"}}),
            ("deref-ok", {"dt-xf2:c": {"item": [{"k": "a", "v": 7}], "pick": "a",
                                       "via": "x"}}),
            ("deref-bad", {"dt-xf2:c": {"item": [{"k": "a", "v": 8}], "pick": "a",
                                        "via": "x"}}),
        ],
    ),
    dict(
        name="instance-identifier-resolution",
        yang=M("dt-iir", """
  list srv { key "name"; leaf name { type string; } leaf v { type uint8; } }
  container c { leaf on { type boolean; } }
  leaf strict { type instance-identifier; }
  leaf loose { type instance-identifier { require-instance false; } }
"""),
        docs=[
            ("existing-list-entry", {
                "dt-iir:srv": [{"name": "a", "v": 1}],
                "dt-iir:strict": "/dt-iir:srv[name='a']/v"}),
            ("absent-instance", {
                "dt-iir:srv": [{"name": "a", "v": 1}],
                "dt-iir:strict": "/dt-iir:srv[name='zz']/v"}),
            ("absent-leaf", {
                "dt-iir:c": {"on": True},
                "dt-iir:strict": "/dt-iir:srv[name='a']/v"}),
            ("schema-invalid-node", {
                "dt-iir:strict": "/dt-iir:nosuch"}),
            ("loose-absent-ok", {
                "dt-iir:loose": "/dt-iir:srv[name='zz']/v"}),
            ("loose-schema-invalid", {
                "dt-iir:loose": "/dt-iir:nosuch"}),
            ("existing-container-leaf", {
                "dt-iir:c": {"on": True},
                "dt-iir:strict": "/dt-iir:c/on"}),
        ],
    ),
    dict(
        name="present-empty-presence",
        yang=M("dt-pep", """
  container feature {
    presence "feature enabled";
    leaf tuning { type uint8; }
  }
  container strictbox {
    presence "on";
    leaf req { type string; mandatory true; }
  }
  leaf gated { type string; must "boolean(../feature)"; }
"""),
        docs=[
            ("present-empty", {"dt-pep:feature": {}}),
            ("present-with-data", {"dt-pep:feature": {"tuning": 3}}),
            ("present-empty-satisfies-must", {"dt-pep:feature": {},
                                              "dt-pep:gated": "x"}),
            ("absent-fails-must", {"dt-pep:gated": "x"}),
            ("present-empty-mandatory-violated", {"dt-pep:strictbox": {}}),
        ],
    ),
]

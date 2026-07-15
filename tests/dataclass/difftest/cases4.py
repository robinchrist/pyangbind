"""Differential test corpus, batch 4: adversarial cases."""
from cases import M


CASES = [
    dict(
        name="xsd-dollar-caret",
        yang=M("dt-anchor", """
  leaf d { type string { pattern "a$b"; } }
  leaf c { type string { pattern "^ab"; } }
  leaf mixed { type string { pattern "[$^]+"; } }
"""),
        docs=[
            ("dollar-literal", {"dt-anchor:d": "a$b"}),   # XSD: $ is literal
            ("dollar-missing", {"dt-anchor:d": "ab"}),
            ("caret-literal", {"dt-anchor:c": "^ab"}),    # XSD: ^ is literal
            ("caret-missing", {"dt-anchor:c": "ab"}),
            ("class-ok", {"dt-anchor:mixed": "$^"}),
        ],
    ),
    dict(
        name="refine",
        yang=M("dt-refine", """
  grouping g {
    container box {
      leaf a { type string; }
      leaf b { type uint8; }
    }
  }
  uses g {
    refine "box/a" { mandatory true; }
    refine "box/b" { default 7; }
  }
  container second {
    uses g {
      refine box { presence "on"; }
    }
  }
"""),
        docs=[
            ("mandatory-via-refine-missing", {"dt-refine:box": {"b": 1}}),
            ("mandatory-via-refine-ok", {"dt-refine:box": {"a": "x", "b": 1}}),
            ("refined-presence-empty-second", {"dt-refine:box": {"a": "x"},
                                               "dt-refine:second": {"box": {"b": 2}}}),
        ],
    ),
    dict(
        name="same-module-augment",
        yang=M("dt-aug", """
  container base { leaf x { type string; } }
  augment "/base" {
    leaf added { type uint8 { range "1..5"; } }
    container extra { leaf deep { type string; mandatory true; } }
  }
"""),
        docs=[
            ("aug-ok", {"dt-aug:base": {"x": "v", "added": 3,
                                        "extra": {"deep": "d"}}}),
            ("aug-range", {"dt-aug:base": {"x": "v", "added": 9}}),
            ("aug-mandatory-propagates", {"dt-aug:base": {"x": "v"}}),
        ],
    ),
    dict(
        name="union-of-unions",
        yang=M("dt-uu", """
  typedef inner { type union { type uint8 { range "1..5"; } type enumeration { enum low; } } }
  leaf u {
    type union {
      type inner;
      type string { pattern "[A-Z]{2}"; }
    }
  }
"""),
        docs=[
            ("inner-int", {"dt-uu:u": 3}),
            ("inner-enum", {"dt-uu:u": "low"}),
            ("outer-str", {"dt-uu:u": "AB"}),
            ("none", {"dt-uu:u": "abc"}),
            ("int-out-of-all", {"dt-uu:u": 9}),
        ],
    ),
    dict(
        name="leafref-in-union",
        yang=M("dt-lru", """
  list srv { key "name"; leaf name { type string; } }
  leaf pick {
    type union {
      type leafref { path "../srv/name"; }
      type uint16;
    }
  }
"""),
        docs=[
            ("ref-ok", {"dt-lru:srv": [{"name": "a"}], "dt-lru:pick": "a"}),
            ("ref-dangling", {"dt-lru:srv": [{"name": "a"}], "dt-lru:pick": "b"}),
            ("num", {"dt-lru:srv": [{"name": "a"}], "dt-lru:pick": 80}),
        ],
    ),
    dict(
        name="decimal64-extremes",
        yang=M("dt-dec", """
  leaf f18 { type decimal64 { fraction-digits 18; } }
  leaf f1min { type decimal64 { fraction-digits 1; range "min..max"; } }
  leaf neg { type decimal64 { fraction-digits 2; range "-10.5..-0.25"; } }
"""),
        docs=[
            ("f18-ok", {"dt-dec:f18": "0.000000000000000001"}),
            ("f18-max", {"dt-dec:f18": "9.223372036854775807"}),
            ("f18-over", {"dt-dec:f18": "9.223372036854775808"}),
            ("f1-min-bound", {"dt-dec:f1min": "-922337203685477580.8"}),
            ("neg-ok", {"dt-dec:neg": "-1.25"}),
            ("neg-out", {"dt-dec:neg": "0.00"}),
        ],
    ),
    dict(
        name="choice-default-case",
        yang=M("dt-cdef", """
  container c {
    leaf pad { type string; }
    choice mode { default plain;
      case plain { leaf p { type string; } }
      case secure {
        leaf cert { type string; mandatory true; }
        leaf key { type string; }
      }
    }
  }
"""),
        docs=[
            ("nothing-selected", {"dt-cdef:c": {"pad": "x"}}),
            ("secure-partial", {"dt-cdef:c": {"pad": "x", "key": "k"}}),
            ("secure-full", {"dt-cdef:c": {"pad": "x", "key": "k", "cert": "c"}}),
        ],
    ),
    dict(
        name="keys-exotic",
        yang=M("dt-keys", """
  list l {
    key "on kind";
    leaf on { type boolean; }
    leaf kind { type enumeration { enum a; enum b; } }
    leaf v { type uint8; }
  }
"""),
        docs=[
            ("ok", {"dt-keys:l": [{"on": True, "kind": "a", "v": 1},
                                  {"on": False, "kind": "a", "v": 2}]}),
            ("dup", {"dt-keys:l": [{"on": True, "kind": "b"},
                                   {"on": True, "kind": "b"}]}),
            ("missing-key", {"dt-keys:l": [{"on": True, "v": 3}]}),
        ],
    ),
    dict(
        name="leaflist-defaults-minelem",
        yang=M("dt-lld", """
  leaf-list tags {
    type string;
    default "a";
    default "b";
  }
  container c {
    leaf-list req { type uint8; min-elements 2; max-elements 3; }
    leaf pad { type string; }
  }
"""),
        docs=[
            ("defaults-untouched", {"dt-lld:c": {"pad": "x", "req": [1, 2]}}),
            ("too-few", {"dt-lld:c": {"pad": "x", "req": [1]}}),
            ("too-many", {"dt-lld:c": {"pad": "x", "req": [1, 2, 3, 4]}}),
        ],
    ),
    dict(
        name="xpath-outside-subset",
        yang=M("dt-xos", """
  container c {
    leaf a { type string; }
    leaf b {
      type string;
      must "re-match(., '[a-z]+')";
    }
  }
"""),
        docs=[
            ("rematch-ok", {"dt-xos:c": {"b": "abc"}}),
            ("rematch-bad", {"dt-xos:c": {"b": "ABC"}}),
        ],
    ),
    dict(
        name="string-escapes-pattern",
        yang=M("dt-esc", """
  leaf tabbed { type string { pattern 'a\\tb'; } }
  leaf branch { type string { pattern 'cat|dog'; } }
  leaf quantified { type string { pattern 'a{2,3}'; } }
"""),
        docs=[
            ("tab-ok", {"dt-esc:tabbed": "a\tb"}),
            ("tab-bad", {"dt-esc:tabbed": "a b"}),
            ("branch-full-only", {"dt-esc:branch": "cat"}),
            ("branch-partial", {"dt-esc:branch": "catx"}),
            ("quant-ok", {"dt-esc:quantified": "aaa"}),
            ("quant-bad", {"dt-esc:quantified": "aaaa"}),
        ],
    ),
]

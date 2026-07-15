"""Differential test corpus, batch 3: XPath engine semantics."""
from cases import M


CASES = [
    dict(
        name="xpath-functions",
        yang=M("dt-xf", """
  container c {
    leaf name { type string; must "string-length(.) >= 3"; }
    leaf tag { type string; must "starts-with(., 'v')"; }
    leaf n { type uint8; must ". mod 2 = 0 and . div 2 < 10"; }
    leaf combo { type string; must "contains(../name, .)"; }
  }
"""),
        docs=[
            ("ok", {"dt-xf:c": {"name": "abcd", "tag": "v1", "n": 4, "combo": "bc"}}),
            ("len-bad", {"dt-xf:c": {"name": "ab"}}),
            ("starts-bad", {"dt-xf:c": {"tag": "x1"}}),
            ("mod-bad", {"dt-xf:c": {"n": 3}}),
            ("contains-bad", {"dt-xf:c": {"name": "abcd", "combo": "zz"}}),
        ],
    ),
    dict(
        name="xpath-nodeset-compare",
        yang=M("dt-xnc", """
  list peer { key "name"; leaf name { type string; } leaf group { type string; } }
  leaf active-group {
    type string;
    must "../peer/group = .";
  }
"""),
        docs=[
            ("some-node-matches", {
                "dt-xnc:peer": [{"name": "a", "group": "g1"}, {"name": "b", "group": "g2"}],
                "dt-xnc:active-group": "g2"}),
            ("no-node-matches", {
                "dt-xnc:peer": [{"name": "a", "group": "g1"}],
                "dt-xnc:active-group": "g9"}),
            ("empty-nodeset", {"dt-xnc:active-group": "g1"}),
        ],
    ),
    dict(
        name="xpath-numeric-strings",
        yang=M("dt-xns", """
  leaf s { type string; }
  leaf n { type uint8; must ". = ../s"; }
"""),
        docs=[
            ("num-vs-string-eq", {"dt-xns:s": "7", "dt-xns:n": 7}),
            ("num-vs-string-ne", {"dt-xns:s": "8", "dt-xns:n": 7}),
            ("non-numeric-string", {"dt-xns:s": "x", "dt-xns:n": 7}),
        ],
    ),
    dict(
        name="xpath-predicates",
        yang=M("dt-xp", """
  list vlan { key "id"; leaf id { type uint16; } leaf role { type string; } }
  leaf mgmt-vlan {
    type uint16;
    must "../vlan[id = current()]/role = 'mgmt'";
  }
"""),
        docs=[
            ("ok", {"dt-xp:vlan": [{"id": 10, "role": "mgmt"}, {"id": 20, "role": "data"}],
                    "dt-xp:mgmt-vlan": 10}),
            ("wrong-role", {"dt-xp:vlan": [{"id": 10, "role": "data"}],
                            "dt-xp:mgmt-vlan": 10}),
            ("no-such-vlan", {"dt-xp:vlan": [{"id": 10, "role": "mgmt"}],
                              "dt-xp:mgmt-vlan": 99}),
        ],
    ),
    dict(
        name="xpath-boolean-coercion",
        yang=M("dt-xbc", """
  leaf flag { type boolean; }
  leaf gated { type string; must "../flag = 'true'"; }
  leaf gated2 { type string; must "../flag"; }
"""),
        docs=[
            ("true-str-eq", {"dt-xbc:flag": True, "dt-xbc:gated": "x"}),
            ("false-str-eq", {"dt-xbc:flag": False, "dt-xbc:gated": "x"}),
            ("exists-check-false-value", {"dt-xbc:flag": False, "dt-xbc:gated2": "x"}),
            ("exists-check-absent", {"dt-xbc:gated2": "x"}),
        ],
    ),
    dict(
        name="union-decimal-bits",
        yang=M("dt-udb", """
  leaf u {
    type union {
      type decimal64 { fraction-digits 1; range "0.0..5.0"; }
      type enumeration { enum off; }
    }
  }
  leaf ub {
    type union {
      type bits { bit x; bit y; }
      type uint8;
    }
  }
"""),
        docs=[
            ("dec-ok", {"dt-udb:u": "3.5"}),
            ("dec-out-of-range", {"dt-udb:u": "9.5"}),
            ("enum-ok", {"dt-udb:u": "off"}),
            ("bits-ok", {"dt-udb:ub": "x y"}),
            ("bits-bad-name", {"dt-udb:ub": "x z"}),
            ("num-ok", {"dt-udb:ub": 7}),
        ],
    ),
    dict(
        name="when-chained",
        yang=M("dt-wc", """
  leaf a { type string; }
  container c1 {
    when "../a = 'on'";
    leaf b { type string; }
    container c2 {
      when "../b = 'deep'";
      leaf d { type string; }
    }
  }
"""),
        docs=[
            ("all-true", {"dt-wc:a": "on", "dt-wc:c1": {"b": "deep", "c2": {"d": "x"}}}),
            ("outer-false", {"dt-wc:a": "off", "dt-wc:c1": {"b": "deep", "c2": {"d": "x"}}}),
            ("inner-false", {"dt-wc:a": "on", "dt-wc:c1": {"b": "flat", "c2": {"d": "x"}}}),
        ],
    ),
    dict(
        name="count-and-paths",
        yang=M("dt-cp", """
  container box {
    list item { key "k"; leaf k { type string; } leaf v { type uint8; } }
    leaf limit {
      type uint8;
      must "count(../item[v > current()]) = 0" {
        error-message "an item exceeds the limit";
      }
    }
  }
"""),
        docs=[
            ("ok", {"dt-cp:box": {"item": [{"k": "a", "v": 3}], "limit": 5}}),
            ("exceeds", {"dt-cp:box": {"item": [{"k": "a", "v": 9}], "limit": 5}}),
        ],
    ),
]

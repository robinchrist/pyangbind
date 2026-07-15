"""Differential test corpus. Each case: one module, several documents."""


def M(name, body):
    return (
        "module %s {\n  yang-version 1.1;\n  namespace \"urn:%s\";\n"
        "  prefix t;\n%s\n}\n" % (name, name, body)
    )


CASES = [
    # ------------------------------------------------- mandatory propagation
    dict(
        name="mand-propagation",
        yang=M("dt-mand", """
  list item {
    key "name";
    leaf name { type string; }
    container np { leaf rt { type string; mandatory true; } }
    container p { presence "on"; leaf rt { type string; mandatory true; } }
    container npc {
      choice enc { mandatory true;
        case a { leaf a { type string; } }
        case b { leaf b { type string; } } }
    }
  }
"""),
        docs=[
            ("empty-entry", {"dt-mand:item": [{"name": "x"}]}),
            ("satisfied", {"dt-mand:item": [{"name": "x", "np": {"rt": "r"}, "npc": {"a": "v"}}]}),
            ("presence-set-invalid", {"dt-mand:item": [{"name": "x", "np": {"rt": "r"}, "npc": {"a": "v"}, "p": {}}]}),
        ],
    ),
    # ------------------------------------------------------------- patterns
    dict(
        name="patterns",
        yang=M("dt-pat", """
  leaf anchored { type string { pattern "ab+"; } }
  leaf multi { type string { pattern "[a-z]+"; pattern ".{3,5}"; } }
  leaf inverted { type string { pattern "x.*" { modifier invert-match; } } }
  leaf classes { type string { pattern "\\\\d+-\\\\w+"; } }
"""),
        docs=[
            ("anchored-ok", {"dt-pat:anchored": "abb"}),
            ("anchored-substring", {"dt-pat:anchored": "zabbz"}),  # XSD: full match required
            ("multi-both-ok", {"dt-pat:multi": "abcd"}),
            ("multi-one-fails", {"dt-pat:multi": "ab"}),
            ("invert-ok", {"dt-pat:inverted": "yes"}),
            ("invert-fails", {"dt-pat:inverted": "xyz"}),
            ("classes-ok", {"dt-pat:classes": "12-ab"}),
            ("classes-bad", {"dt-pat:classes": "12_ab"}),
        ],
    ),
    # --------------------------------------------------------------- ranges
    dict(
        name="ranges",
        yang=M("dt-range", """
  leaf multi { type int8 { range "1..3 | 7"; } }
  leaf big { type uint64; }
  leaf dec { type decimal64 { fraction-digits 2; range "-1.5..2.25"; } }
"""),
        docs=[
            ("multi-in", {"dt-range:multi": 7}),
            ("multi-gap", {"dt-range:multi": 5}),
            ("big-str", {"dt-range:big": "18446744073709551615"}),
            ("big-number-not-string", {"dt-range:big": 5}),  # RFC 7951: must be string
            ("dec-ok", {"dt-range:dec": "2.25"}),
            ("dec-out", {"dt-range:dec": "2.26"}),
            ("dec-too-precise", {"dt-range:dec": "1.234"}),
        ],
    ),
    # -------------------------------------------------------------- lengths
    dict(
        name="lengths",
        yang=M("dt-len", """
  leaf s { type string { length "2..4"; } }
  leaf b { type binary { length "3"; } }
"""),
        docs=[
            ("s-ok", {"dt-len:s": "abc"}),
            ("s-short", {"dt-len:s": "a"}),
            ("b-ok", {"dt-len:b": "AAAA"}),      # base64 of 3 octets
            ("b-wrong", {"dt-len:b": "AAA="}),   # base64 of 2 octets
        ],
    ),
    # ---------------------------------------------------------------- union
    dict(
        name="union",
        yang=M("dt-union", """
  leaf u { type union { type int32; type string { pattern "[a-z]+"; } } }
  leaf ue { type union { type enumeration { enum one; } type uint8; } }
"""),
        docs=[
            ("u-int", {"dt-union:u": 10}),
            ("u-int-as-string", {"dt-union:u": "10"}),  # matches int member per 7951 lenient rules
            ("u-str", {"dt-union:u": "abc"}),
            ("u-neither", {"dt-union:u": "ABC"}),
            ("ue-enum", {"dt-union:ue": "one"}),
            ("ue-num", {"dt-union:ue": 7}),
            ("ue-bad", {"dt-union:ue": "two"}),
        ],
    ),
    # -------------------------------------------------------------- leafref
    dict(
        name="leafref",
        yang=M("dt-lref", """
  list srv { key "name"; leaf name { type string; } }
  leaf active { type leafref { path "../srv/name"; } }
  leaf loose { type leafref { path "../srv/name"; require-instance false; } }
"""),
        docs=[
            ("ok", {"dt-lref:srv": [{"name": "a"}], "dt-lref:active": "a"}),
            ("dangling", {"dt-lref:srv": [{"name": "a"}], "dt-lref:active": "b"}),
            ("loose-dangling", {"dt-lref:srv": [{"name": "a"}], "dt-lref:loose": "b"}),
        ],
    ),
    # ------------------------------------------------------------- when/must
    dict(
        name="when-must",
        yang=M("dt-wm", """
  leaf mode { type string; }
  leaf speed {
    type uint32;
    when "../mode = 'fast'";
  }
  container box {
    leaf x { type uint8; }
    leaf y { type uint8; must ". > ../x"; }
  }
"""),
        docs=[
            ("when-ok", {"dt-wm:mode": "fast", "dt-wm:speed": 10}),
            ("when-false-present", {"dt-wm:mode": "slow", "dt-wm:speed": 10}),
            ("must-ok", {"dt-wm:box": {"x": 1, "y": 2}}),
            ("must-bad", {"dt-wm:box": {"x": 2, "y": 1}}),
        ],
    ),
    # --------------------------------------------------------------- choice
    dict(
        name="choice",
        yang=M("dt-choice", """
  container c {
    choice transport {
      case tcp { leaf tcp-port { type uint16; } }
      case udp { leaf udp-port { type uint16; } }
    }
    choice nested-holder {
      case outer-a {
        choice inner { mandatory true;
          case ia { leaf ia { type string; } }
          case ib { leaf ib { type string; } } }
        leaf marker { type string; }
      }
      case outer-b { leaf ob { type string; } }
    }
  }
"""),
        docs=[
            ("both-cases", {"dt-choice:c": {"tcp-port": 1, "udp-port": 2}}),
            ("one-case", {"dt-choice:c": {"tcp-port": 1}}),
            ("nested-marker-only", {"dt-choice:c": {"marker": "m"}}),  # selects outer-a; inner mandatory unmet
            ("nested-ok", {"dt-choice:c": {"marker": "m", "ia": "x"}}),
        ],
    ),
    # --------------------------------------------------------------- unique
    dict(
        name="unique",
        yang=M("dt-uniq", """
  list srv {
    key "name";
    unique "ip port";
    leaf name { type string; }
    leaf ip { type string; }
    leaf port { type uint16; }
  }
"""),
        docs=[
            ("dup", {"dt-uniq:srv": [
                {"name": "a", "ip": "1.1.1.1", "port": 80},
                {"name": "b", "ip": "1.1.1.1", "port": 80}]}),
            ("one-missing", {"dt-uniq:srv": [
                {"name": "a", "ip": "1.1.1.1"},
                {"name": "b", "ip": "1.1.1.1", "port": 80}]}),
            ("distinct", {"dt-uniq:srv": [
                {"name": "a", "ip": "1.1.1.1", "port": 80},
                {"name": "b", "ip": "1.1.1.1", "port": 81}]}),
        ],
    ),
    # ------------------------------------------------------- lists and keys
    dict(
        name="lists",
        yang=M("dt-list", """
  list l {
    key "k";
    min-elements 1;
    max-elements 2;
    leaf k { type string; }
  }
  leaf-list ll { type uint8; }
"""),
        docs=[
            ("dup-key", {"dt-list:l": [{"k": "a"}, {"k": "a"}]}),
            ("too-many", {"dt-list:l": [{"k": "a"}, {"k": "b"}, {"k": "c"}]}),
            ("too-few", {"dt-list:ll": [1]}),
            ("ll-dup", {"dt-list:l": [{"k": "a"}], "dt-list:ll": [1, 1]}),
            ("ok", {"dt-list:l": [{"k": "a"}], "dt-list:ll": [1, 2]}),
        ],
    ),
    # ------------------------------------------- empty, bits, enum, boolean
    dict(
        name="scalars",
        yang=M("dt-scal", """
  leaf e { type empty; }
  leaf flags { type bits { bit b1; bit b2 { position 5; } } }
  leaf color { type enumeration { enum red; enum blue; } }
  leaf ok { type boolean; }
"""),
        docs=[
            ("empty-ok", {"dt-scal:e": [None]}),
            ("empty-bad", {"dt-scal:e": True}),
            ("bits-ok", {"dt-scal:flags": "b1 b2"}),
            ("bits-bad", {"dt-scal:flags": "b1 b9"}),
            ("enum-ok", {"dt-scal:color": "red"}),
            ("enum-bad", {"dt-scal:color": "green"}),
            ("bool-ok", {"dt-scal:ok": True}),
            ("bool-string", {"dt-scal:ok": "true"}),  # RFC 7951: must be JSON boolean
        ],
    ),
    # ---------------------------------------------------------- identityref
    dict(
        name="identityref",
        yang=M("dt-ident", """
  identity crypto;
  identity aes { base crypto; }
  identity des { base crypto; }
  leaf algo { type identityref { base crypto; } }
"""),
        docs=[
            ("qualified", {"dt-ident:algo": "dt-ident:aes"}),
            ("bare", {"dt-ident:algo": "aes"}),
            ("base-itself", {"dt-ident:algo": "dt-ident:crypto"}),  # base is not derived from itself... it IS derived-or-self; RFC 7950: value must be derived from base -> base itself NOT valid
            ("unknown", {"dt-ident:algo": "dt-ident:rsa"}),
        ],
    ),
    # ------------------------------------------------------------- defaults
    dict(
        name="defaults-choice",
        yang=M("dt-def", """
  container c {
    choice speed { default fast;
      case fast { leaf fast-v { type uint8; } }
      case slow { leaf slow-v { type uint8; } }
    }
    leaf pad { type string; }
  }
"""),
        docs=[
            ("nothing", {"dt-def:c": {"pad": "p"}}),
            ("non-default-case", {"dt-def:c": {"pad": "p", "slow-v": 1}}),
        ],
    ),
]

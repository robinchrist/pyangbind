"""Differential test corpus, batch 2: exotic corners."""
from cases import M


CASES = [
    dict(
        name="leafref-predicate",
        yang=M("dt-lrefp", """
  list srv {
    key "name role";
    leaf name { type string; }
    leaf role { type string; }
  }
  list use {
    key "id";
    leaf id { type string; }
    leaf want-role { type string; }
    leaf pick {
      type leafref {
        path "../../srv[role=current()/../want-role]/name";
      }
    }
  }
"""),
        docs=[
            ("pred-ok", {
                "dt-lrefp:srv": [{"name": "a", "role": "db"}],
                "dt-lrefp:use": [{"id": "u", "want-role": "db", "pick": "a"}]}),
            ("pred-role-mismatch", {
                "dt-lrefp:srv": [{"name": "a", "role": "web"}],
                "dt-lrefp:use": [{"id": "u", "want-role": "db", "pick": "a"}]}),
        ],
    ),
    dict(
        name="typedef-chain",
        yang=M("dt-tdc", """
  typedef word { type string { pattern "[a-z]+"; length "1..10"; } }
  typedef short-word { type word { length "1..4"; } }
  leaf w { type short-word { pattern ".*a.*"; } }
  typedef pct { type uint8 { range "0..100"; } }
  typedef mid { type pct { range "40..60"; } }
  leaf p { type mid; }
"""),
        docs=[
            ("w-ok", {"dt-tdc:w": "abca"}),
            ("w-too-long-for-derived", {"dt-tdc:w": "aaaaaa"}),
            ("w-missing-own-pattern", {"dt-tdc:w": "bcd"}),
            ("w-uppercase", {"dt-tdc:w": "Abc"}),
            ("p-ok", {"dt-tdc:p": 50}),
            ("p-base-only", {"dt-tdc:p": 80}),
        ],
    ),
    dict(
        name="when-on-case",
        yang=M("dt-woc", """
  leaf mode { type string; }
  container c {
    choice speed {
      case fast {
        when "../mode = 'f'";
        leaf fast-v { type uint8; }
      }
      case slow { leaf slow-v { type uint8; } }
    }
  }
"""),
        docs=[
            ("case-when-true", {"dt-woc:mode": "f", "dt-woc:c": {"fast-v": 1}}),
            ("case-when-false", {"dt-woc:mode": "s", "dt-woc:c": {"fast-v": 1}}),
        ],
    ),
    dict(
        name="identity-depth",
        yang=M("dt-idd", """
  identity crypto;
  identity aes { base crypto; }
  identity aes256 { base aes; }
  identity other;
  leaf algo { type identityref { base crypto; } }
"""),
        docs=[
            ("two-levels", {"dt-idd:algo": "dt-idd:aes256"}),
            ("unrelated", {"dt-idd:algo": "dt-idd:other"}),
        ],
    ),
    dict(
        name="if-feature",
        yang=M("dt-iff", """
  feature turbo;
  leaf boost { if-feature turbo; type uint8; }
  leaf plain { type uint8; }
"""),
        docs=[
            ("feature-gated", {"dt-iff:boost": 1}),
            ("ungated", {"dt-iff:plain": 1}),
        ],
    ),
    dict(
        name="unicode-length",
        yang=M("dt-uni", """
  leaf s { type string { length "1..3"; } }
"""),
        docs=[
            ("astral", {"dt-uni:s": "\U0001F600\U0001F600\U0001F600"}),
            ("astral-too-long", {"dt-uni:s": "\U0001F600\U0001F600\U0001F600\U0001F600"}),
        ],
    ),
    dict(
        name="xsd-classes",
        yang=M("dt-xsd", """
  leaf letters { type string { pattern "\\\\p{L}+"; } }
  leaf spaced { type string { pattern "a\\\\s?b"; } }
"""),
        docs=[
            ("ascii", {"dt-xsd:letters": "abc"}),
            ("umlaut", {"dt-xsd:letters": "äö"}),
            ("digit", {"dt-xsd:letters": "a1"}),
            ("space-ok", {"dt-xsd:spaced": "a b"}),
            ("tab", {"dt-xsd:spaced": "a\tb"}),
        ],
    ),
    dict(
        name="binary-bad-b64",
        yang=M("dt-b64", """
  leaf b { type binary; }
"""),
        docs=[
            ("ok", {"dt-b64:b": "AAECAw=="}),
            ("invalid-b64", {"dt-b64:b": "!!notbase64!!"}),
        ],
    ),
    dict(
        name="must-count",
        yang=M("dt-mc", """
  container box {
    must "count(item) <= 2";
    list item { key "k"; leaf k { type string; } }
    leaf-list vals { type uint8; }
    must "not(vals) or count(vals) mod 2 = 0";
  }
"""),
        docs=[
            ("ok", {"dt-mc:box": {"item": [{"k": "a"}], "vals": [1, 2]}}),
            ("too-many", {"dt-mc:box": {"item": [{"k": "a"}, {"k": "b"}, {"k": "c"}]}}),
            ("odd-vals", {"dt-mc:box": {"item": [{"k": "a"}], "vals": [1]}}),
        ],
    ),
    dict(
        name="min-in-case",
        yang=M("dt-mic", """
  container c {
    choice pick {
      case many { leaf-list tags { type string; min-elements 2; } leaf m { type string; } }
      case one  { leaf single { type string; } }
    }
  }
"""),
        docs=[
            ("other-case", {"dt-mic:c": {"single": "s"}}),
            ("case-active-too-few", {"dt-mic:c": {"m": "x", "tags": ["a"]}}),
            ("case-active-enough", {"dt-mic:c": {"m": "x", "tags": ["a", "b"]}}),
        ],
    ),
    dict(
        name="string-number-range",
        yang=M("dt-snr", """
  leaf n { type int32 { range "min..-1 | 10..max"; } }
"""),
        docs=[
            ("neg", {"dt-snr:n": -5}),
            ("gap", {"dt-snr:n": 5}),
            ("high", {"dt-snr:n": 2147483647}),
        ],
    ),
    dict(
        name="instance-identifier",
        yang=M("dt-ii", """
  leaf target { type instance-identifier { require-instance false; } }
"""),
        docs=[
            ("ok-path", {"dt-ii:target": "/dt-ii:target"}),
            ("garbage", {"dt-ii:target": "not a path"}),
        ],
    ),
]

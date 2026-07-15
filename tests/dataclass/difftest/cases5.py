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
]

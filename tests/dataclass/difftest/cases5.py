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
]

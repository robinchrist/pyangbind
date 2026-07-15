#!/usr/bin/env python3
"""Differential tester: pybind-dataclass validate_tree vs libyang.

Every case is one YANG module plus JSON instance documents; each
document is validated by yanglint (libyang, `-t config`) and by the
generated bindings (from_ietf_json + validate_tree), and any verdict
disagreement is reported with both sides' diagnostics. Our side counts
decode errors and on-assignment validation errors as REJECT, since
libyang folds parsing and validation into one verdict too.

Requires `yanglint` and `pyang` on PATH; exits 0 (skip) without them.
Usage: run.py [substring-filter] [-v]
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import types

BASE = os.path.dirname(os.path.abspath(__file__))
PYANGBIND = os.path.abspath(os.path.join(BASE, "..", "..", ".."))
PLUGIN_DIR = os.path.join(PYANGBIND, "pyangbind", "plugin")
sys.path.insert(0, BASE)


def class_name(module_name):
    return "".join(p.capitalize() for p in re.split(r"[-_.]", module_name))


def gen_bindings(yang_path, workdir):
    cmd = [
        shutil.which("pyang"), "--plugindir", PLUGIN_DIR, "-f", "pybind-dataclass",
        "--dataclass-serde", "-p", workdir, yang_path,
    ]
    env = dict(os.environ, PYTHONPATH=PYANGBIND)
    out = subprocess.run(cmd, capture_output=True, env=env)
    if out.returncode != 0:
        raise RuntimeError("pyang failed:\n%s" % out.stderr.decode())
    module = types.ModuleType("difftest_bindings")
    sys.modules[module.__name__] = module
    try:
        exec(compile(out.stdout, "difftest_bindings.py", "exec"), module.__dict__)
    finally:
        del sys.modules[module.__name__]
    return module


def ours(bindings, root_cls_name, doc):
    try:
        tree = bindings.from_ietf_json(getattr(bindings, root_cls_name), doc)
        bindings.validate_tree(tree)
        return True, ""
    except Exception as exc:  # decode or validation failure both = reject
        return False, "%s: %s" % (type(exc).__name__, exc)


def libyang(yang_path, doc, workdir):
    data_path = os.path.join(workdir, "data.json")
    with open(data_path, "w") as fh:
        # raw UTF-8: libyang's JSON parser does not take escaped
        # surrogate pairs, and RFC 7951 data is UTF-8 anyway
        json.dump(doc, fh, ensure_ascii=False)
    out = subprocess.run(
        ["yanglint", "-t", "config", yang_path, data_path],
        capture_output=True,
    )
    return out.returncode == 0, out.stderr.decode().strip()


def run_case(case, verbose=False):
    name, yang, docs = case["name"], case["yang"], case["docs"]
    module_name = re.search(r"module\s+([\w.-]+)", yang).group(1)
    root_cls = class_name(module_name)
    disagreements = []
    with tempfile.TemporaryDirectory() as workdir:
        yang_path = os.path.join(workdir, "%s.yang" % module_name)
        with open(yang_path, "w") as fh:
            fh.write(yang)
        try:
            bindings = gen_bindings(yang_path, workdir)
        except RuntimeError as exc:
            return [(name, "<codegen>", "CODEGEN FAILED", str(exc))]
        for docname, doc in docs:
            ok_ours, diag_ours = ours(bindings, root_cls, doc)
            ok_ly, diag_ly = libyang(yang_path, doc, workdir)
            if verbose:
                print("  %s/%s: ours=%s libyang=%s" % (name, docname, ok_ours, ok_ly))
            if ok_ours != ok_ly:
                disagreements.append((
                    name, docname,
                    "ours=%s libyang=%s" % (
                        "accept" if ok_ours else "reject",
                        "accept" if ok_ly else "reject"),
                    "OURS: %s\nLIBYANG: %s" % (diag_ours or "-", diag_ly or "-"),
                ))
    return disagreements


def main(cases, only=None, verbose=False):
    total_docs = 0
    all_disagreements = []
    for case in cases:
        if only and only not in case["name"]:
            continue
        total_docs += len(case["docs"])
        all_disagreements += run_case(case, verbose=verbose)
    print("=" * 70)
    print("%d documents checked, %d disagreement(s)" % (total_docs, len(all_disagreements)))
    for name, docname, verdict, diag in all_disagreements:
        print("-" * 70)
        print("[%s / %s] %s" % (name, docname, verdict))
        for line in diag.splitlines():
            print("    " + line)
    return 1 if all_disagreements else 0


if __name__ == "__main__":
    if shutil.which("yanglint") is None or shutil.which("pyang") is None:
        print("yanglint / pyang not on PATH; skipping differential tests")
        sys.exit(0)
    import cases as batch1
    import cases2 as batch2
    import cases3 as batch3
    import cases4 as batch4
    import cases5 as batch5

    all_cases = batch1.CASES + batch2.CASES + batch3.CASES + batch4.CASES + batch5.CASES
    only = next((a for a in sys.argv[1:] if a != "-v"), None)
    sys.exit(main(all_cases, only=only, verbose="-v" in sys.argv))

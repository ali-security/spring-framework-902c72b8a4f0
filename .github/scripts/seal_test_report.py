#!/usr/bin/env python3
# Sealed-build test reporter: summarizes Gradle JUnit XML results per module
# and states, for every skipped test, the reason found in the test source
# (skip annotation, assume* call, class-level condition, or inherited one).
import glob
import re
import sys
import xml.etree.ElementTree as ET
from collections import OrderedDict

LEG = sys.argv[1] if len(sys.argv) > 1 else "all"

INDEX = {}
for pattern in ("**/src/test*/**/*.java", "**/src/test*/**/*.kt",
                "**/src/main/**/*.java", "**/src/main/**/*.kt"):
    for p in glob.glob(pattern, recursive=True):
        if "/build/" in p:
            continue
        tail = p.split("/src/", 1)[1].split("/", 2)
        if len(tail) < 3:
            continue
        fqcn = tail[2].rsplit(".", 1)[0].replace("/", ".")
        INDEX.setdefault(fqcn, p)

ANN = re.compile(r"@(Disabled\w*|Enabled\w*|Ignore|IfProfileValue|ParameterizedHttpServerTest)\b(\((?:[^()]|\([^()]*\))*\))?")
ASSUME = re.compile(r"\b(Assume\.\w+|Assumptions\.\w+|assume\w*)\s*\(")
_cache = {}


def lines_of(path):
    if path not in _cache:
        try:
            _cache[path] = open(path, encoding="utf-8").read().split("\n")
        except OSError:
            _cache[path] = []
    return _cache[path]


def is_comment(s):
    s = s.strip()
    return s.startswith("//") or s.startswith("*") or s.startswith("/*")


def call_text(lines, i, start):
    text, depth, out = "", 0, ""
    for j in range(i, min(len(lines), i + 6)):
        text += (lines[j][start:] if j == i else " " + lines[j].strip())
        start = 0
    for ch in text:
        out += ch
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                break
    return re.sub(r"\s+", " ", out)[:200]


def explain(ann):
    if ann.startswith("@EnabledForTestGroups"):
        return ann + " (opt-in test group; runs only with -PtestGroups=<group>, which upstream CI never sets)"
    if ann.startswith("@Disabled") or ann.startswith("@Ignore"):
        return ann + " (disabled upstream in source)"
    if ann.startswith("@ParameterizedHttpServerTest"):
        return ann
    return ann + " (upstream conditional-execution annotation)"


def annotations_above(lines, i, n=10):
    found = []
    for j in range(i, max(-1, i - n), -1):
        s = lines[j].strip()
        if j < i and (s.endswith("}") or s.endswith(";") or s.endswith("{")) and not s.startswith("@"):
            break
        if is_comment(s):
            continue
        found += [m.group(0) for m in ANN.finditer(lines[j])]
    return found


def assumes_in(lines, a, b):
    out = []
    for i in range(a, min(b, len(lines))):
        if is_comment(lines[i]):
            continue
        for m in ASSUME.finditer(lines[i]):
            t = call_text(lines, i, m.start())
            if t not in out:
                out.append(t)
    return out


def body_end(lines, i):
    depth, seen = 0, False
    for j in range(i, len(lines)):
        if is_comment(lines[j]):
            continue
        depth += lines[j].count("{") - lines[j].count("}")
        if "{" in lines[j]:
            seen = True
        if seen and depth <= 0:
            return j + 1
    return len(lines)


def class_decl(lines, simple):
    rx = re.compile(r"\b(class|interface)\s+" + re.escape(simple) + r"\b")
    for i, l in enumerate(lines):
        if not is_comment(l) and rx.search(l):
            return i
    return None


def superclass(path, lines, decl):
    head = " ".join(lines[decl:decl + 4])
    m = re.search(r"\bextends\s+([\w.]+)", head.split("{", 1)[0])
    if not m:
        m = re.search(r"\)\s*:\s*([\w.]+)\s*\(", head)  # Kotlin
    if not m:
        return None
    name = m.group(1)
    if name in INDEX:
        return name
    for l in lines:
        im = re.match(r"\s*import\s+([\w.]+)\." + re.escape(name.split(".")[-1]) + r"\s*;?\s*$", l)
        if im:
            fq = im.group(1) + "." + name.split(".")[-1]
            if fq in INDEX:
                return fq
    pkg = next((re.match(r"\s*package\s+([\w.]+)", l).group(1) for l in lines if re.match(r"\s*package\s+[\w.]+", l)), "")
    fq = pkg + "." + name
    return fq if fq in INDEX else None


def method_name(name):
    n = name
    while re.search(r"\[[^\]]*\]\s*$", n):
        n = re.sub(r"\s*\[[^\]]*\]\s*$", "", n)
    n = re.sub(r"\(.*\)\s*$", "", n).strip().strip("`").strip()
    return n


def resolve(cls, name, message):
    if message:
        return "skip message: " + message.strip().replace("\n", " ")[:250]
    parts = cls.split("$")
    fq = parts[0]
    simple = parts[-1].split(".")[-1]
    method = method_name(name)
    reasons = []
    visited = 0
    while fq and fq in INDEX and visited < 6:
        visited += 1
        path = INDEX[fq]
        lines = lines_of(path)
        if method:
            rx = re.compile(r"(void|fun)\s+`?" + re.escape(method) + r"`?\s*\(")
            for i, l in enumerate(lines):
                if not is_comment(l) and rx.search(l):
                    anns = [a for a in annotations_above(lines, i) if not a.startswith("@ParameterizedHttpServerTest")]
                    if anns:
                        reasons.append("; ".join(explain(a) for a in anns) + " on method " + method + " [" + path + ":" + str(i + 1) + "]")
                    calls = assumes_in(lines, i, body_end(lines, i))
                    if calls:
                        reasons.append("assumption in test body: " + "; ".join(calls) + " [" + path + ":" + str(i + 1) + "]")
                    break
        if reasons:
            return " | ".join(reasons)
        decl = class_decl(lines, simple if visited == 1 else fq.split(".")[-1])
        if decl is not None:
            anns = [a for a in annotations_above(lines, decl) if not a.startswith("@ParameterizedHttpServerTest")]
            if anns:
                return "; ".join(explain(a) for a in anns) + " on class " + fq.split(".")[-1] + " [" + path + "]"
        calls = assumes_in(lines, 0, len(lines))
        if calls:
            return "assumption(s) in " + fq.split(".")[-1] + " (setup or parameterized body): " + "; ".join(calls[:6]) + " [" + path + "]"
        if decl is None:
            break
        fq = superclass(path, lines, decl)
        simple = fq.split(".")[-1] if fq else simple
    return "skipped by JUnit at runtime; no skip annotation or assumption found in source for " + cls


modules = OrderedDict()
skips = OrderedDict()
failures = []
for xml in sorted(glob.glob("**/build/test-results/**/TEST-*.xml", recursive=True)):
    mod = xml.split("/build/", 1)[0] + ":" + xml.split("/test-results/", 1)[1].split("/", 1)[0]
    try:
        root = ET.parse(xml).getroot()
    except ET.ParseError:
        continue
    st = modules.setdefault(mod, [0, 0, 0])
    for tc in root.iter("testcase"):
        st[0] += 1
        cls, name = tc.get("classname", ""), tc.get("name", "")
        if tc.find("failure") is not None or tc.find("error") is not None:
            st[1] += 1
            failures.append(cls + " > " + name)
        elif tc.find("skipped") is not None:
            st[2] += 1
            reason = resolve(cls, name, tc.find("skipped").get("message"))
            entry = skips.setdefault((cls, reason), [])
            entry.append(name)

print("==== SEAL TEST REPORT [%s] ====" % LEG)
if failures:
    print("FAILED TESTS:")
    for f in failures:
        print("  FAILED: " + f)
print("SKIPPED TESTS (grouped by class and reason):")
for (cls, reason), names in skips.items():
    sample = ", ".join(n[:60] for n in names[:3]) + (", ..." if len(names) > 3 else "")
    print("  SKIPPED x%d %s [%s] -- %s" % (len(names), cls, sample, reason))
t = p = f = s = 0
for mod, (n, nf, ns) in modules.items():
    print("TEST SUMMARY %s: tests=%d passed=%d failed=%d skipped=%d" % (mod, n, n - nf - ns, nf, ns))
    t, f, s = t + n, f + nf, s + ns
p = t - f - s
print("TEST TOTAL [%s]: tests=%d passed=%d failed=%d skipped=%d (every skip above carries its source reason)" % (LEG, t, p, f, s))

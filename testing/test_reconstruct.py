#!/usr/bin/env python3
"""Reconstruction unit-test harness for draftwatch (spec section 11.4/11.5 invariants).

For each scenario: create a real git repo, commit a baseline, write a working
version, run the real `git diff --word-diff=porcelain`, parse it with draftwatch's
parser, and assert:
    reconstruct(accept-all) == working file   (spec test 4)
    reconstruct(reject-all) == baseline file   (spec test 5)

Trailing-newline differences of at most one char are tolerated (spec 11.4).
Pure blank-line insert/delete is a documented known limitation and is reported,
not asserted.
"""
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import draftwatch as wt

PASS = 0
FAIL = 0
NOTES = []


def sh(args, cwd):
    subprocess.run(args, cwd=cwd, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def make_repo():
    d = tempfile.mkdtemp(prefix="wt-test-")
    sh(["git", "init", "-q"], d)
    sh(["git", "config", "user.email", "t@t.com"], d)
    sh(["git", "config", "user.name", "t"], d)
    return d


def norm_trailing(a, b):
    """Return True if a == b modulo a single trailing newline."""
    if a == b:
        return True
    if a.rstrip("\n") == b.rstrip("\n") and abs(len(a) - len(b)) <= 1:
        return True
    return False


def all_decisions(hunks, value):
    return {str(h): value for h in hunks}


def run_case(name, baseline_text, working_text, expect_exact=True):
    global PASS, FAIL
    d = make_repo()
    try:
        f = os.path.join(d, "draft.md")
        with open(f, "w", newline="") as fh:
            fh.write(baseline_text)
        sh(["git", "add", "draft.md"], d)
        sh(["git", "commit", "-qm", "init"], d)
        with open(f, "w", newline="") as fh:
            fh.write(working_text)

        baseline = {"kind": "head", "ref": "HEAD", "label": "HEAD"}
        diff_text = wt.git_diff_text(d, baseline, "draft.md", f)
        segments, hunks = wt.parse_word_diff(diff_text)

        acc = wt.reconstruct(segments, all_decisions(hunks, "accept"), pending_is_accept=True)
        rej = wt.reconstruct(segments, all_decisions(hunks, "reject"), pending_is_accept=True)

        ok_acc = (acc == working_text) if expect_exact else norm_trailing(acc, working_text)
        ok_rej = (rej == baseline_text) if expect_exact else norm_trailing(rej, baseline_text)

        status = "PASS" if (ok_acc and ok_rej) else "FAIL"
        if ok_acc and ok_rej:
            PASS += 1
        else:
            FAIL += 1
        print("[{}] {}  (hunks={})".format(status, name, len(hunks)))
        if not ok_acc:
            print("    accept-all != working")
            print("      got:      %r" % acc)
            print("      expected: %r" % working_text)
        if not ok_rej:
            print("    reject-all != baseline")
            print("      got:      %r" % rej)
            print("      expected: %r" % baseline_text)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def run_no_change(name, text):
    """No diff -> zero segments. Apply must be a no-op (must NOT truncate the file)."""
    global PASS, FAIL
    d = make_repo()
    try:
        f = os.path.join(d, "draft.md")
        with open(f, "w", newline="") as fh:
            fh.write(text)
        sh(["git", "add", "draft.md"], d)
        sh(["git", "commit", "-qm", "init"], d)
        baseline = {"kind": "head", "ref": "HEAD", "label": "HEAD"}
        diff_text = wt.git_diff_text(d, baseline, "draft.md", f)
        segments, hunks = wt.parse_word_diff(diff_text)
        ok = (segments == [] and hunks == [])
        if ok:
            PASS += 1
        else:
            FAIL += 1
        print("[{}] {}  (segments={}, hunks={}; apply is a no-op)".format(
            "PASS" if ok else "FAIL", name, len(segments), len(hunks)))
    finally:
        shutil.rmtree(d, ignore_errors=True)


def run_known_limitation(name, baseline_text, working_text):
    """Report (not assert) the pure blank-line ambiguity."""
    d = make_repo()
    try:
        f = os.path.join(d, "draft.md")
        with open(f, "w", newline="") as fh:
            fh.write(baseline_text)
        sh(["git", "add", "draft.md"], d)
        sh(["git", "commit", "-qm", "init"], d)
        with open(f, "w", newline="") as fh:
            fh.write(working_text)
        baseline = {"kind": "head", "ref": "HEAD", "label": "HEAD"}
        diff_text = wt.git_diff_text(d, baseline, "draft.md", f)
        segments, hunks = wt.parse_word_diff(diff_text)
        acc = wt.reconstruct(segments, all_decisions(hunks, "accept"), pending_is_accept=True)
        rej = wt.reconstruct(segments, all_decisions(hunks, "reject"), pending_is_accept=True)
        NOTES.append((name, acc == working_text, rej == baseline_text))
        print("[NOTE] {}  accept==working:{}  reject==baseline:{}".format(
            name, acc == working_text, rej == baseline_text))
    finally:
        shutil.rmtree(d, ignore_errors=True)


def main():
    print("=== reconstruction harness (git %s) ===" %
          subprocess.run(["git", "--version"], capture_output=True, text=True).stdout.strip())

    # 1. in-line word substitution
    run_case("substitution",
             "the quick brown fox\njumps over\nthe lazy dog\n",
             "the quick red fox\njumps over\nthe lazy dog\n")

    # 2. content line inserted
    run_case("content-line insert",
             "the quick brown fox\njumps over\nthe lazy dog\n",
             "the quick brown fox\njumps over\nNEW LINE HERE\nthe lazy dog\n")

    # 3. content line deleted
    run_case("content-line delete",
             "the quick brown fox\njumps over\nthe lazy dog\n",
             "the quick brown fox\nthe lazy dog\n")

    # 4. multi-line append at end
    run_case("multi-line append",
             "alpha\nbeta\n",
             "alpha\nbeta\ngamma\ndelta\nepsilon\n")

    # 5. multiple separate substitutions (two hunks)
    run_case("two hunks",
             "the quick brown fox\njumps over\nthe lazy dog\n",
             "the slow brown fox\njumps over\nthe sleepy dog\n")

    # 6. mixed: one line replaced, one inserted, one deleted (no shared words at
    #    change boundaries, so no word-realignment whitespace ambiguity)
    run_case("mixed replace/insert/delete",
             "apple\nbanana\ncherry\ndate\n",
             "apple\nBANANA_NEW\ninserted_xyz\ndate\n")

    # 7. trailing newline removed (tolerated, not exact)
    run_case("trailing-newline removal",
             "the quick brown fox\njumps over\nthe lazy dog\n",
             "the quick brown fox\njumps over\nthe lazy dog",
             expect_exact=False)

    # 8. word change on a no-trailing-newline file
    run_case("word change, file already lacked trailing newline",
             "alpha\nbeta\ngamma",
             "alpha\nBETA\ngamma",
             expect_exact=False)

    # 9. empty diff (no change) -> zero segments; apply is a no-op (never writes).
    run_no_change("no change", "same\ntext\nhere\n")

    # 10. literal word-diff marker sequences in unchanged text. With plain
    #     --word-diff this was the one case where even *accept* did not round-trip
    #     (unescapable [-/{+ markers). Porcelain prefixes every token, so this is
    #     now a hard requirement, not a known limitation.
    run_case("literal marker in unchanged text",
             "a [-x-] note\nchange me\n",
             "a [-x-] note\nCHANGED\n")

    # known limitations (reported, not asserted; documented in README):
    #   (a) pure blank-line insert/delete is ambiguous even in porcelain: git
    #       emits identical bodies (a bare `~`) for both; only the @@ header
    #       counts differ, so per-newline siding is still unknowable.
    run_known_limitation("pure blank-line INSERT",
                          "AAA\nBBB\n", "AAA\n\nBBB\n")
    run_known_limitation("pure blank-line DELETE",
                          "AAA\n\nBBB\n", "AAA\nBBB\n")
    #   (b) word-realignment: a shared word across a change boundary shifts
    #       surrounding whitespace; the reject/baseline side may be byte-inexact.
    #       (Identical under porcelain — the alignment is git's, not the format's.)
    run_known_limitation("word-realignment (shared word at boundary)",
                          "line one\nline two\nline three\nline four\n",
                          "line ONE\nline two\ninserted line\nline four\n")

    print("\n=== %d passed, %d failed ===" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())

from src.agents.screener import isolate_fragment, scan, split_diff_into_fragments


def test_clean_code_no_match():
    result = scan("def add(a, b):\n    return a + b\n")
    assert result.matched is False
    assert result.matched_buckets == []


def test_pii_to_llm_matches_imports_and_pii():
    bad = '''
import openai

def get_opinion(client):
    prompt = f"{client.first_name}, SSN {client.ssn}, income {client.annual_income}"
    return openai.chat.completions.create(model="gpt-4", messages=[{"role": "user", "content": prompt}])
'''
    result = scan(bad)
    assert result.matched is True
    assert "imports" in result.matched_buckets
    assert "pii_data" in result.matched_buckets


def test_logging_sink_bucket_pii_to_logs():
    result = scan('logging.info(f"Processing applicant {client.ssn}, income {client.annual_income}")')
    assert result.matched is True
    assert "logging_sinks" in result.matched_buckets
    assert "pii_data" in result.matched_buckets


def test_cache_sink_bucket_pii_to_cache():
    result = scan('redis.set(f"user:{client.email}", client.date_of_birth)')
    assert result.matched is True
    assert "cache_sinks" in result.matched_buckets
    assert "pii_data" in result.matched_buckets


def test_decision_mutation_bucket():
    result = scan('loan_account.status = "Approved"')
    assert result.matched is True
    assert "decision_mutation" in result.matched_buckets


def test_bias_data_bucket():
    result = scan("score -= 5 if client.pincode in high_risk_zones else 0")
    assert result.matched is True
    assert "bias_data" in result.matched_buckets


def test_agentic_bucket():
    result = scan("subprocess.run(['rm', '-rf', path])")
    assert result.matched is True
    assert "agentic" in result.matched_buckets


def test_no_false_positive_from_race_substring():
    # regression test for review finding #63 — "traceback"/"embrace" used
    # to trip bias_data's "race" via plain substring matching
    result = scan("try:\n    do_thing()\nexcept Exception:\n    log.error(traceback.format_exc())\n")
    assert "bias_data" not in result.matched_buckets
    result2 = scan("def embrace_new_design():\n    pass\n")
    assert "bias_data" not in result2.matched_buckets


def test_no_false_positive_from_mobile_substring():
    # regression test for review finding #63 — "automobile" used to trip
    # pii_data's "mobile" via plain substring matching
    result = scan("loan_purpose = 'automobile financing for the applicant'")
    assert "pii_data" not in result.matched_buckets


def test_real_mobile_field_still_matches():
    # confirms the word-boundary fix didn't overcorrect into false negatives
    result = scan("logging.info(client.mobile)")
    assert "pii_data" in result.matched_buckets


def test_punctuation_ending_terms_match_with_their_own_argument_immediately_following():
    # Regression test for a bug an independent review caught (2026-09-05)
    # in the #63 word-boundary fix itself: an unconditional (?!\w) after
    # every term broke matching for any term ending in punctuation
    # ("print(", ".setex(", "eval(", "exec(", "Agent(", "session[") the
    # instant it was followed by its own realistic argument - exactly the
    # normal, correct usage, not an edge case. Reproduced directly before
    # fixing: scan("print(applicant_summary)") returned matched=False.
    cases = [
        ("result = eval(user_input)", "agentic", "eval("),
        ("subprocess.exec(cmd)", "agentic", "exec("),
        ("agent = Agent(tools=[shell_tool])", "agentic", "Agent("),
        ("print(applicant_summary)", "logging_sinks", "print("),
        ("session[user_id] = pii_blob", "cache_sinks", "session["),
        ("cache.setex(300, key, value)", "cache_sinks", ".setex("),
    ]
    for text, expected_bucket, expected_term in cases:
        result = scan(text)
        assert result.matched, f"{text!r} should have matched but didn't"
        assert expected_bucket in result.matched_buckets, f"{text!r} should be in {expected_bucket}"
        assert expected_term in result.matched_terms, f"{text!r} should report term {expected_term!r}"


def test_isolate_fragment_keeps_only_relevant_lines():
    code = "\n".join([f"line_{i} = {i}" for i in range(20)] + ["ssn_value = client.ssn"])
    result = scan(code)
    fragment = isolate_fragment(code, result.matched_terms, context_lines=1)
    assert "ssn_value" in fragment
    assert "line_0 " not in fragment  # far from the match, should be excluded


MULTI_FILE_DIFF = """diff --git a/loans/a.py b/loans/a.py
new file mode 100644
index 0000000..1111111
--- /dev/null
+++ b/loans/a.py
@@ -0,0 +1,10 @@
+import subprocess
+from strands import Agent, tool
+
+@tool
+def run_cmd(command):
+    return subprocess.run(command, shell=True)
+
+agent = Agent(tools=[run_cmd])
+
+def clean_helper():
+    return 1 + 1
diff --git a/loans/b.py b/loans/b.py
new file mode 100644
index 0000000..2222222
--- /dev/null
+++ b/loans/b.py
@@ -0,0 +1,3 @@
+def score(applicant):
+    if applicant.pincode in RISKY:
+        return -50
"""


def test_split_diff_into_fragments_separates_files():
    fragments = split_diff_into_fragments(MULTI_FILE_DIFF)
    files = {f["file"] for f in fragments}
    assert files == {"loans/a.py", "loans/b.py"}


def test_split_diff_into_fragments_separates_functions_within_a_file():
    fragments = split_diff_into_fragments(MULTI_FILE_DIFF)
    a_fragments = [f["text"] for f in fragments if f["file"] == "loans/a.py"]
    # run_cmd/agent vs clean_helper should land in different fragments
    assert any("run_cmd" in t and "clean_helper" not in t for t in a_fragments)
    assert any("clean_helper" in t and "run_cmd" not in t for t in a_fragments)


def test_split_diff_into_fragments_falls_back_without_git_markers():
    fragments = split_diff_into_fragments("just some code, no diff --git header")
    assert len(fragments) == 1
    assert fragments[0]["file"] == "<diff>"


def test_split_diff_preserves_code_before_first_function():
    # regression test for review finding #5 — code before the first
    # def/class (imports, module-level constants) used to be silently
    # dropped from every fragment
    diff = """diff --git a/loans/x.py b/loans/x.py
new file mode 100644
--- /dev/null
+++ b/loans/x.py
@@ -0,0 +1,6 @@
+import openai
+
+def first_func():
+    return 1
+
+def second_func():
+    return 2
"""
    fragments = split_diff_into_fragments(diff)
    assert any("import openai" in f["text"] for f in fragments)


def test_split_diff_does_not_split_indented_methods_from_their_class():
    # regression test for review finding #64 — indented class methods used
    # to be treated as top-level boundaries too, splitting a class's
    # __init__ away from a method that uses what __init__ built
    diff = """diff --git a/loans/y.py b/loans/y.py
new file mode 100644
--- /dev/null
+++ b/loans/y.py
@@ -0,0 +1,7 @@
+class Scorer:
+    def __init__(self):
+        self.weights = build_weights()
+
+    def score(self, applicant):
+        if applicant.pincode in RISKY:
+            return self.weights['penalty']
"""
    fragments = split_diff_into_fragments(diff)
    # the whole class should stay in one fragment — no top-level def/class
    # boundary exists inside it (only indented methods), so this should not
    # split at all
    assert len(fragments) == 1
    assert "__init__" in fragments[0]["text"]
    assert "def score" in fragments[0]["text"]

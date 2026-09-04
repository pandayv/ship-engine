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

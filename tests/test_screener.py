from src.agents.screener import isolate_fragment, scan


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

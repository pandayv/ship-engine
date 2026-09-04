"""
Amazon Comprehend PII confirmation pass — sits between Screener's regex/AST
match and Diagnostician's full semantic call, per the decided architecture
(replacing the originally-considered Microsoft Presidio). Only called on
fragments Screener has already flagged, preserving the "don't spend on
clean code" tiered-cost design.

Worth noting honestly: Comprehend's DetectPiiEntities is built for natural-
language text/documents, not source code — it's genuinely untested whether
it adds real signal on a Python code fragment (variable names, f-strings)
versus prose. Treat its output as a confidence signal to enrich
Diagnostician's input, not a gate that skips Diagnostician on its own.
"""

from dataclasses import dataclass


@dataclass
class PiiFinding:
    entity_type: str
    score: float
    text: str


def detect_pii(text: str, language_code: str = "en") -> list[PiiFinding]:
    import boto3  # lazy import, same pattern as vector_store.py/alert_store.py

    client = boto3.client("comprehend")
    response = client.detect_pii_entities(Text=text, LanguageCode=language_code)
    findings = []
    for entity in response.get("Entities", []):
        snippet = text[entity["BeginOffset"]:entity["EndOffset"]]
        findings.append(PiiFinding(entity_type=entity["Type"], score=entity["Score"], text=snippet))
    return findings


if __name__ == "__main__":
    from src.agents.screener import isolate_fragment, scan

    real_fragment = '''
+import openai
+
+client_api = openai.OpenAI()
+
+        f"Applicant: {client.first_name} {client.last_name}\\n"
+        f"DOB: {client.date_of_birth}\\n"
+        f"Gender: {client.gender}\\n"
+        f"Blood group: {client.blood_group}\\n"
+        f"Occupation: {client.occupation}\\n"
+        f"Annual income: {client.annual_income}\\n"
+        f"Mobile: {client.mobile}\\n"
+        f"Pincode: {client.pincode}\\n"
'''
    findings = detect_pii(real_fragment)
    print(f"Comprehend found {len(findings)} PII entities in the real PR #1 fragment:")
    for f in findings:
        print(f"  {f.entity_type} (score {f.score:.2f}): {f.text!r}")

    # comparison case: plain prose containing the same kind of content, to
    # see if Comprehend performs meaningfully differently on natural
    # language vs. this code-shaped text
    prose_equivalent = (
        "Applicant: John Smith. DOB: 1990-04-12. Gender: Male. "
        "Blood group: O+. Occupation: Engineer. Annual income: 85000. "
        "Mobile: 9876543210. Pincode: 560001."
    )
    prose_findings = detect_pii(prose_equivalent)
    print(f"\nFor comparison, {len(prose_findings)} PII entities found in equivalent PLAIN PROSE:")
    for f in prose_findings:
        print(f"  {f.entity_type} (score {f.score:.2f}): {f.text!r}")

"""Extract each normative DoD bullet into a stable requirement/evidence index.

Existing test/evidence annotations are retained by content hash, never by line number.
No test execution automatically promotes a release requirement.
"""

import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "docs/definition-of-done.md"
TARGET = ROOT / "docs/implementation/traceability.json"


def generate() -> dict[str, object]:
    previous = json.loads(TARGET.read_text()) if TARGET.exists() else {"requirements": []}
    prior = {item["requirement_digest"]: item for item in previous["requirements"]}
    lines = SOURCE.read_text().splitlines()
    requirements = []
    section = "contract"
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith("##"):
            section = line.lstrip("# ")
        if line.startswith("- "):
            source_line = index + 1
            paragraph = [line[2:]]
            index += 1
            while index < len(lines) and lines[index].startswith("  "):
                paragraph.append(lines[index].strip())
                index += 1
            text = " ".join(paragraph)
            content_digest = hashlib.sha256(text.encode()).hexdigest()
            existing = prior.get(content_digest, {})
            section_number = re.match(r"(\d+)\.", section)
            section_id = section_number[1] if section_number else "contract"
            requirements.append(
                {
                    "requirement_id": "DOD-" + section_id + "-" + content_digest[:10],
                    "requirement_digest": content_digest,
                    "source": "docs/definition-of-done.md",
                    "source_line": source_line,
                    "section": section,
                    "requirement": text,
                    "state": existing.get("state", "IMPLEMENTING"),
                    "tests": existing.get("tests", []),
                    "evidence": existing.get("evidence", []),
                    "remaining": existing.get(
                        "remaining", "Implementation and required evidence pending."
                    ),
                }
            )
            continue
        index += 1
    result = {
        "schema_version": 1,
        "source_digest": hashlib.sha256(SOURCE.read_bytes()).hexdigest(),
        "release_state": "IMPLEMENTING",
        "required_tag": "v1.0.0",
        "interpretation": "Every bullet is indexed; surrounding contract text remains binding.",
        "requirements": requirements,
    }
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    return result


if __name__ == "__main__":
    result = generate()
    requirements = result["requirements"]
    if not isinstance(requirements, list):
        raise ValueError("Traceability generation did not produce a requirement list.")
    print(json.dumps({"requirements": len(requirements), "state": "IMPLEMENTING"}))

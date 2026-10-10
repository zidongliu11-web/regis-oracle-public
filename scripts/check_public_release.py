"""Check public snapshot hygiene and docs, without reading user data or networking."""
from __future__ import annotations
import ast
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP = {".git", ".venv", "venv", "__pycache__", ".pytest_cache"}
BLOCKED_SUFFIXES = {".csv", ".xlsx", ".parquet", ".pdf", ".db", ".sqlite", ".zip", ".pkl", ".key", ".pem", ".p12", ".pfx"}
PRIVATE_PATH = re.compile(r"C:[/\\]Users[/\\]|\x2fUsers\x2f[^/\s]+\x2f|\x2fhome\x2f[^/\s]+\x2f")
SECRETS = [
    re.compile(r"(?:ghp_|github_pat_)[A-Za-z0-9_]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"sk-[A-Za-z0-9]{32,}"),
]
def check(root=ROOT):
    errors=[]; python_count=0; file_count=0; local_links=0
    required=["README.md","LICENSE","CONTRIBUTING.md","SECURITY.md","CODE_OF_CONDUCT.md","CHANGELOG.md","THIRD_PARTY_NOTICES.md","docs/assets/regis-banner.svg",".github/workflows/public-checks.yml"]
    for rel in required:
        if not (root/rel).is_file():errors.append({"path":rel,"reason":"required_file_missing"})
    for path in sorted(root.rglob("*")):
        rel=path.relative_to(root)
        if any(part in SKIP for part in rel.parts) or not path.is_file():continue
        file_count+=1
        if path.suffix.lower() in BLOCKED_SUFFIXES or path.name.startswith(".env") and path.name!=".env.example":
            errors.append({"path":rel.as_posix(),"reason":"non_public_file_type"});continue
        if any(p in {"state","cache","registry","archives","data","workflow_receipts",".codex",".codex-task"} for p in rel.parts):
            errors.append({"path":rel.as_posix(),"reason":"internal_runtime_directory"})
        try:content=path.read_text("utf-8-sig")
        except UnicodeError:
            errors.append({"path":rel.as_posix(),"reason":"unexpected_binary"});continue
        if PRIVATE_PATH.search(content):
            errors.append({"path":rel.as_posix(),"reason":"private_machine_path"})
        if any(p.search(content) for p in SECRETS):
            errors.append({"path":rel.as_posix(),"reason":"credential_pattern"})
        if path.suffix==".py":
            python_count+=1
            try:ast.parse(content)
            except SyntaxError:errors.append({"path":rel.as_posix(),"reason":"python_syntax"})
        if path.suffix==".md":
            for target in re.findall(r"\]\(([^)]+)\)",content):
                if re.match(r"(?:[a-zA-Z]+:|#)",target):continue
                target=target.split("#",1)[0].strip("<>")
                if not target:continue
                local_links+=1
                if not (path.parent/target).exists():errors.append({"path":rel.as_posix(),"reason":"broken_local_link","target":target})
    return {"files_checked":file_count,"python_files":python_count,"local_document_links":local_links,"errors":errors,
            "scope":"snapshot_hygiene_syntax_docs_only_not_full_security_or_financial_validation"}
if __name__=="__main__":
    result=check();print(json.dumps(result,ensure_ascii=False,indent=2));raise SystemExit(bool(result["errors"]))

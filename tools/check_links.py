"""Offline Markdown local-target check; does not claim remote URL validation."""
from pathlib import Path
import re,sys
from urllib.parse import unquote,urlsplit
ROOT=Path(__file__).resolve().parents[1]
errors=[];checked=0
for source in ROOT.rglob('*.md'):
 if any(part in ('.git','.venv','.pytest_cache') for part in source.parts):continue
 text=re.sub(r'```.*?```','',source.read_text(encoding='utf-8'),flags=re.S)
 for match in re.finditer(r'!?\[[^\]]*\]\(([^\s)]+)(?:\s+"[^"]*")?\)',text):
  raw=match.group(1).strip('<>');parts=urlsplit(raw)
  if parts.scheme or raw.startswith('#'):continue
  checked+=1;target=(source.parent/unquote(parts.path)).resolve()
  if not target.is_relative_to(ROOT) or not target.exists():errors.append(f'{source.relative_to(ROOT)}: {raw}')
print(f'Local Markdown targets checked: {checked}; broken: {len(errors)}')
for error in errors:print(error)
raise SystemExit(bool(errors))

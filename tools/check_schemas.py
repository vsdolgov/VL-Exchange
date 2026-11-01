from pathlib import Path
import json
from jsonschema import Draft202012Validator
ROOT=Path(__file__).resolve().parents[1];count=0
for directory in (ROOT/'model/spec',ROOT/'scenario/spec',ROOT/'specs',ROOT/'attribution'):
 for file in directory.rglob('*.schema.json'):
  Draft202012Validator.check_schema(json.loads(file.read_text()));count+=1
print(f'JSON Schemas checked: {count}')

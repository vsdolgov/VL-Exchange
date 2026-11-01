from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
for path in (ROOT/'model/src',ROOT/'scenario/src',ROOT/'security-runtime/src'):sys.path.insert(0,str(path))

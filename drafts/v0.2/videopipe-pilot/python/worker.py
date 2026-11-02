"""JSONL subprocess boundary used by the real C++ VideoPipe adapter."""
import bootstrap
import json
import sys
from runtime import Session


def main():
    session = Session()
    try:
        for line in sys.stdin:
            if not line.strip():
                continue
            command = json.loads(line, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
            response = session.handle(command)
            print(json.dumps(response, ensure_ascii=False, allow_nan=False, separators=(",", ":")), flush=True)
            if command["op"] == "shutdown":
                break
    finally:
        session.shutdown()


if __name__ == "__main__":
    main()

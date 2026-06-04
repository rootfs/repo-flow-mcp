import json
from util import run


def main() -> None:
    run()
    print(json.dumps({"ok": True}))


if __name__ == "__main__":
    main()

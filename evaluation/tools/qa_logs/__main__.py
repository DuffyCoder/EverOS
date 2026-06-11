import sys


def main() -> int:
    print(
        "qa_logs has explicit tools now:\n"
        "  python -m evaluation.tools.qa_logs.dump ...\n"
        "  python -m evaluation.tools.qa_logs.annotate ...\n"
        "  python -m evaluation.tools.qa_logs.materialize_qa_jsonl ...",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())

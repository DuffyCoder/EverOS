from pathlib import Path

from evaluation.tools.qa_logs.cli import parse_args
from evaluation.tools.qa_logs import dump_qa_logs, dump_qa_logs_all_errors


def main() -> int:
    args = parse_args()
    base_out = Path(args.out or f"reports/qa_logs/{args.run_name}")
    if args.qid_mode == "all_errors":
        dump_qa_logs_all_errors(args)
        print(f"[qa_logs] wrote per-qid directories under {base_out}/")
    else:
        out_dir = base_out / args.qid
        dump_qa_logs(args, out_dir)
        print(f"[qa_logs] wrote raw dump under {out_dir}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

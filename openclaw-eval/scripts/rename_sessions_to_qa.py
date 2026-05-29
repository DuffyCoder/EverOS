#!/usr/bin/env python3
"""
rename_sessions_to_qa.py — 把 agent_local 评测产生的 session 转写文件
    artifacts/.../sessions/<uuid>.jsonl.<ts>
复制成可读的
    artifacts/.../sessions/qa<n>.jsonl
其中 n 与 eval_results.json 里的 `locomo_<conv>_qa<n>` 一致(与 locomo10.json 中 qa 列表的索引一致)。

匹配规则(防同前缀错配):
  从 user message 的 </relevant-memories> 之后提取"真实问题文本",
  与 qa.question 做归一化精确匹配(exact > prefix > substring 三级 fallback)。
  同一 qa 有多个 session snapshot 时取最新(timestamp 最大)。

用法:
    python3 rename_sessions_to_qa.py                                  # 默认: main-noproxy-c4 run
    python3 rename_sessions_to_qa.py --run-name main-noproxy-c4
    python3 rename_sessions_to_qa.py --results-dir /path/to/results/locomo-<run-dir>
    python3 rename_sessions_to_qa.py --results-dir <...> --dataset /path/locomo10.json

幂等:每次跑会先清掉旧 qa*.jsonl,再重新生成。
"""
import argparse, glob, json, os, re, shutil, sys

DEFAULT_REPO = "/Data3/shutong.shan/memory/refs/EverMemOS"
DEFAULT_RUN_NAME = "main-noproxy-c4"
DEFAULT_SYSTEM = "locomo-openclaw-docker-openviking-session-bundle-noop"
DEFAULT_DATASET = "evaluation/data/locomo/locomo10.json"


def msg_text(m):
    c = m.get("content", "")
    if isinstance(c, list):
        return " ".join(
            str(x.get("text", "")) if isinstance(x, dict) else str(x) for x in c
        )
    return str(c)


def first_user_msg(path):
    try:
        for line in open(path, encoding="utf-8", errors="ignore"):
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get("type") == "message" and d.get("message", {}).get("role") == "user":
                return msg_text(d["message"])
    except Exception:
        pass
    return None


def extract_tail(umsg):
    """取 </relevant-memories> 之后的文本(真实问题在此);若无则取整段。"""
    parts = umsg.rsplit("</relevant-memories>", 1)
    return parts[1].strip() if len(parts) > 1 else umsg.strip()


def norm(s):
    return re.sub(r"\s+", " ", s.strip().lower())


def find_conversations_dir(results_dir):
    cand = glob.glob(os.path.join(results_dir, "artifacts/openclaw/run-*/conversations"))
    if not cand:
        sys.exit(f"❌ 找不到 conversations 目录于 {results_dir}/artifacts/openclaw/run-*/conversations")
    if len(cand) > 1:
        print(f"⚠️  发现多个 run 目录,取最新:{cand}", file=sys.stderr)
        cand.sort()
    return cand[-1]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default=DEFAULT_REPO, help="EverMemOS 仓库根(默认 %(default)s)")
    ap.add_argument("--run-name", default=DEFAULT_RUN_NAME,
                    help="run 名(默认 %(default)s);与 --system 拼出 results/<system>-<run-name>/")
    ap.add_argument("--system", default=DEFAULT_SYSTEM, help="system 前缀(默认 %(default)s)")
    ap.add_argument("--results-dir",
                    help="直接指定 results 目录(覆盖 --run-name/--system)")
    ap.add_argument("--dataset", help=f"locomo10.json 路径(默认 <repo>/{DEFAULT_DATASET})")
    args = ap.parse_args()

    results_dir = args.results_dir or os.path.join(
        args.repo, "evaluation/results", f"{args.system}-{args.run_name}"
    )
    dataset = args.dataset or os.path.join(args.repo, DEFAULT_DATASET)

    if not os.path.isdir(results_dir):
        sys.exit(f"❌ results 目录不存在: {results_dir}")
    if not os.path.isfile(dataset):
        sys.exit(f"❌ dataset 文件不存在: {dataset}")

    conv_root = find_conversations_dir(results_dir)
    loco = json.load(open(dataset))
    print(f"results: {results_dir}")
    print(f"dataset: {dataset}")
    print(f"conv root: {conv_root}\n")

    RANK = {"exact": 3, "prefix": 2, "substring": 1}
    total = 0
    for ci, conv in enumerate(loco):
        sess_dir = os.path.join(conv_root, f"locomo_{ci}/state/agents/main/sessions")
        if not os.path.isdir(sess_dir):
            print(f"  conv{ci}: (无 sessions 目录,跳过)")
            continue
        qa_list = conv.get("qa", [])
        for old in glob.glob(os.path.join(sess_dir, "qa*.jsonl")):
            os.remove(old)

        files_info = []
        for f in glob.glob(os.path.join(sess_dir, "*.jsonl.*")):
            m = re.search(r"\.(\d+)$", f)
            ts = int(m.group(1)) if m else 0
            umsg = first_user_msg(f)
            if umsg:
                files_info.append((f, ts, norm(extract_tail(umsg))))

        best = {}
        for n, qa in enumerate(qa_list):
            q = (qa.get("question") or "").strip()
            if not q:
                continue
            qn = norm(q)
            cands = []
            for f, ts, tn in files_info:
                if tn == qn:
                    cands.append((ts, f, "exact"))
                elif tn.startswith(qn):
                    cands.append((ts, f, "prefix"))
                elif qn in tn:
                    cands.append((ts, f, "substring"))
            if cands:
                cands.sort(key=lambda x: (RANK[x[2]], x[0]), reverse=True)
                best[n] = cands[0][1]

        for n, src in best.items():
            shutil.copyfile(src, os.path.join(sess_dir, f"qa{n}.jsonl"))
            total += 1
        print(f"  conv{ci}: 写 {len(best):>3} / 总 qa={len(qa_list)}")

    print(f"\n总复制 {total} 个 qa<n>.jsonl")


if __name__ == "__main__":
    main()

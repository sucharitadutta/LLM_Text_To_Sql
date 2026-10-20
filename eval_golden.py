"""Execution-accuracy eval: compare RESULT SETS of generated vs reference SQL."""
import json
from text2sql import answer, run_query


def norm(df):
    return sorted(tuple(map(str, row)) for row in df.round(4).values.tolist())


def main():
    cases = json.load(open("golden_set.json"))
    passed = 0
    for c in cases:
        r = answer(c["question"])
        if r["status"] != "ok":
            print(f"FAIL ({r['status']}): {c['question']}")
            continue
        ref, _ = run_query(c["reference_sql"])
        ok = norm(r["data"]) == norm(ref)
        passed += ok
        print(f"{'PASS' if ok else 'FAIL'}: {c['question']}")
    print(f"\nExecution accuracy: {passed}/{len(cases)} = {passed/len(cases):.0%}")


if __name__ == "__main__":
    main()

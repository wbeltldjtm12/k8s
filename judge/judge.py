import json, os, random, time, sys
from pathlib import Path

WORKSPACE = Path(__file__).parent

try:
    from dotenv import load_dotenv
    load_dotenv(Path(os.environ.get("JUDGE_ENV_FILE", Path(__file__).with_name(".env"))))
except Exception:
    pass

GT_PATH = Path(os.environ.get("GROUND_TRUTH_FILE", WORKSPACE.parent / "eval" / "ground_truth.json"))
CYCLE_PATHS = [Path(value).expanduser().resolve() for value in sys.argv[1:]]
if not CYCLE_PATHS and os.environ.get("JUDGE_CYCLE_DIRS"):
    CYCLE_PATHS = [
        Path(value).expanduser().resolve()
        for value in os.environ["JUDGE_CYCLE_DIRS"].split(os.pathsep)
        if value
    ]
if not CYCLE_PATHS:
    print("Usage: python judge/judge.py <cycle_dir1> [cycle_dir2 ...]")
    sys.exit(1)

if not GT_PATH.exists(): print(f"[ERROR] {GT_PATH}"); sys.exit(1)
with open(GT_PATH, encoding="utf-8") as f: gt = json.load(f)
DETECTABLE = [name for name, value in gt.items() if value.get("detectable")]

from google import genai
from google.genai import types as gtypes
from openai import OpenAI
import anthropic

gemini_key = os.environ.get('GOOGLE_API_KEY') or os.environ.get('GEMINI_API_KEY')
openai_key = os.environ.get('OPENAI_API_KEY')
claude_key = os.environ.get('ANTHROPIC_API_KEY')

missing = []
if not gemini_key: missing.append("GOOGLE_API_KEY / GEMINI_API_KEY")
if not openai_key: missing.append("OPENAI_API_KEY")
if not claude_key: missing.append("ANTHROPIC_API_KEY")
if missing:
    print("[ERROR] API 키 없음:"); [print(f"  - {m}") for m in missing]; sys.exit(1)

gclient = genai.Client(api_key=gemini_key)
oai = OpenAI(api_key=openai_key)
claude = anthropic.Anthropic(api_key=claude_key)
GEMINI_MODEL = os.environ.get("GEMINI_JUDGE_MODEL", "gemini-2.5-flash")
OPENAI_MODEL = os.environ.get("OPENAI_JUDGE_MODEL", "gpt-4o-mini")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_JUDGE_MODEL", "claude-sonnet-4-20250514")

def fmt(data, mode):
    if mode == 'llm_only':
        ai = data.get('data',{}).get('ai_analysis','')
        return ai[:800] if ai else '(결과 없음)'
    chains = data.get('data',{}).get('chains',[])
    if not chains: return '(결과 없음)'
    if mode == 'hybrid':
        parts = [f"[체인{i}] {c.get('root_cause_kind','')}/{c.get('root_cause','')} | {c.get('root_cause_reason','')} | BR:{c.get('blast_radius',0)}" for i,c in enumerate(chains[:3],1)]
        base = "\n".join(parts)
        ai = data.get('data',{}).get('ai_analysis','')
        return base + (f"\n\n[LLM보고서]\n{ai[:1500]}" if ai else "")
    c = chains[0]
    return f"Root Cause: {c.get('root_cause_kind','')}/{c.get('root_cause','')}\nReason: {c.get('root_cause_reason','')}\nBR: {c.get('blast_radius',0)}"

def prompt(gt_desc, shuffled, lmap, mdata):
    parts = [f"=== 방법 {lmap[m]} ===\n{fmt(mdata[m],m)}" for m in shuffled]
    return f"""Kubernetes 전문가로서 공정하게 채점하세요.
=== 장애 시나리오 ===\n{gt_desc}\n\n{chr(10).join(parts)}
=== 기준 ===\n1. Blast Radius 리소스는 K8s 오브젝트\n2. 구조화 출력도 정확하면 고점\n3. (a)근본원인 정확성 (b)전파범위 합리성 (c)즉시조치 가능성
0~10점, JSON만:
{{"A":{{"score":8,"reason":"..."}},"B":{{"score":5,"reason":"..."}},"C":{{"score":0,"reason":"..."}}}}"""

def parse(text):
    text = text.strip()
    if text.startswith("```"): text = text.split("\n",1)[-1].rsplit("```",1)[0]
    if text.startswith("json"): text = text[4:].strip()
    return json.loads(text)

def call_gemini(p):
    for i in range(3):
        try:
            r = gclient.models.generate_content(model=GEMINI_MODEL, contents=p, config=gtypes.GenerateContentConfig(temperature=0.1))
            return parse(r.text)
        except Exception as e: print(f"  Gemini retry {i}: {e}"); time.sleep(5)

def call_gpt(p):
    for i in range(3):
        try:
            r = oai.chat.completions.create(model=OPENAI_MODEL, messages=[{'role':'user','content':p}], temperature=0.1)
            return parse(r.choices[0].message.content)
        except Exception as e: print(f"  GPT4o retry {i}: {e}"); time.sleep(5)

def call_claude(p):
    for i in range(3):
        try:
            r = claude.messages.create(model=ANTHROPIC_MODEL, max_tokens=512, temperature=0.1, messages=[{'role':'user','content':p}])
            return parse(r.content[0].text)
        except Exception as e:
            print(f"  Claude retry {i}: {e}")
            if 'credit balance' in str(e):
                print("  [SKIP] Claude 크레딧 부족 - 스킵")
                return None
            time.sleep(5)

modes  = ['hybrid','dfs_only','llm_only']
judges = [('gemini',call_gemini),('gpt4o',call_gpt),('claude',call_claude)]
scores = {j:{m:[] for m in modes} for j,_ in judges}
details= {j:{} for j,_ in judges}

print(f"{'='*50}\n LLM-as-Judge 5-Cycle 평균 채점\n{'='*50}")
for cyc_path in CYCLE_PATHS:
    cyc = cyc_path.name
    if not cyc_path.exists(): print(f"\n[SKIP] {cyc_path}"); continue
    print(f"\n--- {cyc} ---")
    for sc in DETECTABLE:
        sp = cyc_path/sc
        if not sp.exists(): continue
        status_path = sp / "status.txt"
        if status_path.exists() and "status=valid" not in status_path.read_text(encoding="utf-8").splitlines():
            continue
        mode_paths = {mode: sp / f"{mode}.json" for mode in ('hybrid', 'dfs_only', 'llm_only')}
        if not all(path.is_file() for path in mode_paths.values()):
            print(f"  [SKIP] incomplete mode outputs: {sc}")
            continue
        print(f"\n  [{sc}]")
        gt_desc = gt.get(sc,{}).get('description',sc)
        mdata = {
            mode: json.loads(path.read_text(encoding="utf-8"))
            for mode, path in mode_paths.items()
        }
        if any(payload.get("status") != "success" for payload in mdata.values()):
            print(f"  [SKIP] non-success mode output: {sc}")
            continue
        shuffled = list(modes); random.shuffle(shuffled)
        lmap = {shuffled[i]:chr(65+i) for i in range(len(modes))}
        rmap = {v:k for k,v in lmap.items()}
        p_str = prompt(gt_desc, shuffled, lmap, mdata)
        key = f"{cyc}/{sc}"
        for jn, jf in judges:
            res = jf(p_str)
            if res:
                details[jn][key] = {}
                for ltr, info in res.items():
                    m = rmap.get(ltr)
                    if m:
                        s = info.get('score',0); scores[jn][m].append(s)
                        details[jn][key][m] = {'score':s,'reason':info.get('reason','')}
                print(f"    [{jn}] " + " | ".join(f"{m}:{details[jn][key].get(m,{}).get('score','?')}" for m in modes))
            else: print(f"    [{jn}] FAIL")
            time.sleep(2)

print(f"\n\n{'='*50}\n FINAL RESULTS (5-Cycle 평균)\n{'='*50}")
avgs = {}
for jn,_ in judges:
    print(f"\n--- {jn.upper()} ---")
    avgs[jn] = {}
    for m in modes:
        arr = scores[jn][m]
        avg = sum(arr) / len(arr) if arr else None
        avgs[jn][m] = round(avg, 2) if avg is not None else None
        display = f"{avg:.2f}/10" if avg is not None else "N/A"
        print(f"  {m:<12}: {display}  (n={len(arr)})")

print("\n--- ENSEMBLE ---")
for m in modes:
    vals = [avgs[j][m] for j, _ in judges if avgs[j][m] is not None]
    display = f"{sum(vals) / len(vals):.2f}/10" if vals else "N/A"
    print(f"  {m:<12}: {display}")

default_output = WORKSPACE / f"judge_results_{time.strftime('%Y%m%d_%H%M%S')}.json"
out = Path(os.environ.get("JUDGE_OUTPUT", default_output))
with open(out, "w", encoding="utf-8") as stream:
    json.dump(
        {
            'cycles': [str(path) for path in CYCLE_PATHS],
            'judge_models': {
                'gemini': GEMINI_MODEL, 'gpt4o': OPENAI_MODEL, 'claude': ANTHROPIC_MODEL,
            },
            'averages': avgs, 'scores': scores, 'details': details,
        },
        stream,
        ensure_ascii=False,
        indent=2,
    )
print(f"\n[저장완료] {out}")

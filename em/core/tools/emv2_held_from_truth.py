import glob, json
R = {
 "native": ("Arc native USDC: token-event balances miss native value moves; being reconciled with node balances.",
            "아크 네이티브 USDC는 가스 토큰이라 노드 잔고와 대조 중이에요."),
 "native_gas": ("Native USDC balances differ from node balances by gas fees; being reconciled.",
                "네이티브 USDC 잔고에 가스비가 빠져 있어 노드 잔고와 대조 중이에요."),
 "revert": ("The token contract refuses balance reads (look-alike token); balances cannot be verified.",
            "이 토큰은 잔고 조회가 막혀 있어 확인할 수 없어요(다른 토큰을 흉내 낸 토큰으로 보여요)."),
 "spam": ("Spam token: its contract reports balances that do not follow its transfers.",
          "스팸 토큰이에요. 컨트랙트가 전송 기록과 다른 가짜 잔고를 돌려줘요."),
 "nonstd": ("Non-standard token: node balances do not follow its transfer events (rebasing or special balanceOf).",
            "표준이 아닌 토큰이라 전송 기록과 노드 잔고가 달라요."),
 "interest": ("Interest-bearing lending token: balances grow without transfer events.",
              "이자가 붙는 대출 토큰이라 전송 기록만으로는 잔고가 맞지 않아요."),
}
special = {"arc:0x3600000000000000000000000000000000000000": "native", "arc:0xfffffffffffffffffffffffffffffffffffffffe": "native_gas",
           "arc:0xc6203d86f5efbed7f9512755f3e6132ee50d03d1": "revert", "linea:0xcc22f6aa610d1b2a0e89ef228079cb3e1831b1d1": "nonstd",
           "linea:0xb4ffef15daf4c02787bc5332580b838ce39805f5": "interest"}
lines = ["# chain\ttoken\treason\treasonKo\tclass\tsymbol  (tools/rpc_truth.py FAIL, 2026-10-20; checks/truth/*.json)"]
for c in ("arc", "unichain", "linea"):
    d = json.load(open(sorted(glob.glob(f"/srv/skew/emv2/checks/truth/{c}-*.json"))[-1]))
    for t in d["tokens"]:
        if t["verdict"] != "FAIL": continue
        k = c + ":" + t["token"]
        why = special.get(k, "spam")
        cls = {"native_gas": "native"}.get(why, why)
        lines.append("\t".join((c, t["token"], *R[why], cls, (t["symbol"] or "")[:30].replace("\t", " "))))
open("/srv/skew/emv2/held.tsv.next", "w").write("\n".join(lines) + "\n")
print(len(lines) - 1, "held")

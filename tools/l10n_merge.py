#!/usr/bin/env python3
"""翻译表的 git 合并器（11-09 启动器线：每支新文案都追加在 Localizable.json 文件尾，同批几支依次合入时后合的一律文本冲突、整支退回，
#201 #221 #238 一天被顶掉四次）。.gitattributes 把翻译表交给它（merge=s2a-l10n）；列车启动时在仓库配置里登记：
  git config merge.s2a-l10n.driver 'python3 tools/l10n_merge.py %O %A %B'
不能用 merge=union：两边都在尾部追加时 git 把相同的「},  {」行收掉，并出的 JSON 缺逗号（实测），坏文件还不报冲突。
两种表：
  条目表 [{"source": 中文, "en": 英文}, …]（launchers/macos/adapter/.../Localizable.json）：按 source 认条目；
  键值表 {"键": 文案, …}（launchers/macos/frontend/l10n/*.json）：按键认。
三方合并，逐条：两边一样 → 留；只一边相对基改了（含新增、删除）→ 取改的那边；两边都改且不同 → 退出 1（留给人，同普通冲突）。
输出顺序：本侧（%A）原有条目按原序，对侧（%B）新增的接在后面；缩进与文件尾换行照本侧原样。写回 %A，退出 0。"""
import json, re, sys

GONE = object()


def load(path):
    text = open(path, encoding='utf-8').read()
    return text, json.loads(text)


def entries(doc):
    """→ (形状, 有序 {键: 值})；认不出的形状 → None"""
    if isinstance(doc, dict):
        return 'dict', dict(doc)
    if isinstance(doc, list) and all(isinstance(e, dict) and isinstance(e.get('source'), str) for e in doc):
        keys = [e['source'] for e in doc]
        if len(set(keys)) == len(keys):
            return 'list', {e['source']: e for e in doc}
    return None


def main(o, a, b):
    try:
        (_, do), (ta, da), (_, db) = load(o), load(a), load(b)
    except (OSError, ValueError):
        return 1
    eo, ea, eb = entries(do), entries(da), entries(db)
    if not eo or not ea or not eb or not eo[0] == ea[0] == eb[0]:
        return 1
    shape, mo, ma, mb = ea[0], eo[1], ea[1], eb[1]
    out = {}
    for k in list(ma) + [k for k in mb if k not in ma] + [k for k in mo if k not in ma and k not in mb]:
        vo, va, vb = mo.get(k, GONE), ma.get(k, GONE), mb.get(k, GONE)
        v = va if va == vb else vb if va == vo else va if vb == vo else None
        if v is None:
            return 1
        if v is not GONE:
            out[k] = v
    m = re.match(r'[\[{]\n( +)', ta)
    doc = out if shape == 'dict' else list(out.values())
    text = json.dumps(doc, ensure_ascii=False, indent=len(m.group(1)) if m else 1) + ('\n' if ta.endswith('\n') else '')
    open(a, 'w', encoding='utf-8').write(text)
    return 0


if __name__ == '__main__':
    if len(sys.argv) < 4:
        sys.exit('用法：l10n_merge.py <基> <本侧（写回）> <对侧> [路径]')
    sys.exit(main(*sys.argv[1:4]))

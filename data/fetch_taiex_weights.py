# -*- coding: utf-8 -*-
"""
fetch_taiex_weights.py
======================
抓取 TAIFEX「臺灣證券交易所發行量加權股價指數成分股暨市值比重」，
產生 data/taiex_weights.json，供前端「期貨持倉」卡片的
『台指成分股曝險回推』使用（名目部位 × 成分股權重）。

來源頁面（UTF-8、台灣 IP 可直連，curl 無 UA 會被擋故用 urllib + UA）：
    https://www.taifex.com.tw/cht/2/weightedPropertion
版面為兩欄（a/b）並排，每列含兩檔；本程式抽出全部 (代碼,名稱,權重)，
依權重由大到小排序後取前 N（預設 30）寫檔。

執行：
    python data/fetch_taiex_weights.py
排程：權重每月底更新一次即可；目前掛在 update_fund.py 末尾（GH Actions 每 2h）。
"""

import os, sys, re, json, datetime
import urllib.request

OUT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'taiex_weights.json')
URL = 'https://www.taifex.com.tw/cht/2/weightedPropertion'
TOP_N = 30
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/124.0 Safari/537.36',
    'Referer': 'https://www.taifex.com.tw/cht/index',
    'Accept-Language': 'zh-TW,zh;q=0.9',
}


def http_get(url, timeout=25):
    req = urllib.request.Request(url, headers=HEADERS)
    return urllib.request.urlopen(req, timeout=timeout).read().decode('utf-8', errors='replace')


def parse_weights(html):
    """抽出 (code, name, weight%) 三組有序清單後 zip。

    版面欄位（a/b 兩側結構相同，只差 _a / _b 後綴）：
      headers=rank_[ab]>名次   headers=name_[ab]>代碼   myContent>名稱
      headers=propertion_[ab]>權重%
    三組清單在文件中的出現順序一致（a1,b1,a2,b2,...），故 zip 對齊。
    """
    codes   = re.findall(r'headers=name_[ab]>\s*(\d{3,6})\s*</td>', html, re.I)
    names   = re.findall(r'class="myContent">\s*([^<]+?)\s*</span>', html, re.I)
    weights = re.findall(r'headers=propertion_[ab]>\s*([\d.]+)\s*%', html, re.I)
    n = min(len(codes), len(names), len(weights))
    if n == 0:
        return []
    items = []
    for i in range(n):
        try:
            w = float(weights[i])
        except ValueError:
            continue
        if w <= 0:
            continue
        nm = names[i].strip().rstrip('*').strip()  # 去掉 TAIFEX 註記星號
        items.append({'code': codes[i], 'name': nm, 'w': round(w, 4)})
    # 依權重由大到小
    items.sort(key=lambda x: x['w'], reverse=True)
    return items


def parse_asof(html):
    """頁面上的資料日期（YYYY/MM/DD 或 民國年）。找不到回 None。"""
    m = re.search(r'(20\d{2})[/\-.](\d{1,2})[/\-.](\d{1,2})', html)
    if m:
        y, mo, d = m.group(1), int(m.group(2)), int(m.group(3))
        return f'{y}-{mo:02d}-{d:02d}'
    # 民國年（如 115/05/29）
    m = re.search(r'\b(1\d{2})[/\-.](\d{1,2})[/\-.](\d{1,2})\b', html)
    if m:
        y = int(m.group(1)) + 1911
        return f'{y}-{int(m.group(2)):02d}-{int(m.group(3)):02d}'
    return None


def main():
    try:
        html = http_get(URL)
    except Exception as e:
        print(f'[taiex_weights] 抓取失敗：{type(e).__name__}: {e}', flush=True)
        return 1

    items = parse_weights(html)
    if len(items) < 20:
        print(f'[taiex_weights] 解析到 {len(items)} 檔（<20），疑似版面改版，不覆寫舊檔。', flush=True)
        return 1

    asof = parse_asof(html) or datetime.date.today().isoformat()
    payload = {
        'generated': datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds'),
        'asof': asof,
        'source': 'TAIFEX',
        'url': URL,
        'items': items[:TOP_N],
    }
    with open(OUT_FILE, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write('\n')

    top5 = ', '.join(f'{it["code"]} {it["w"]}%' for it in items[:5])
    print(f'[taiex_weights] OK 資料日 {asof}，共 {len(items)} 檔（寫前 {TOP_N}）。Top5: {top5}', flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())

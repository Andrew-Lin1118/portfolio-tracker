/* Portfolio Tracker – Service Worker
   版本號更新會清除舊快取並重新下載 */
const CACHE_NAME = 'portfolio-tracker-v286';  // 2026-07-01: ETF 成分股曝險金額
const PRECACHE = [
  './portfolio-tracker-v13.html',
  'https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js',
  'https://fonts.googleapis.com/css2?family=Noto+Sans+TC:wght@400;500;700&display=swap'
];

// 安裝：預快取核心資源
self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then(cache => cache.addAll(PRECACHE).catch(() => {}))
      .then(() => self.skipWaiting())
  );
});

// 啟動：清除舊版快取
self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(keys =>
      Promise.all(
        keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k))
      )
    ).then(() => self.clients.claim())
  );
});

// 請求攔截：
//   - HTML 主文件 → 一律走網路，不進快取（避免看到舊版 UI）
//   - fundamentals.json → 網路優先，失敗才回退快取（避免一直看到上一輪舊數據）
//   - 其他資源 → 網路優先，失敗再用快取
self.addEventListener('fetch', event => {
  const url = event.request.url;
  const pathname = new URL(url).pathname;

  // ★ /data/*.json + /proxy?* + 任何含 _nosw 的 URL → SW 完全不介入，瀏覽器直接走網路。
  //   理由：總經頁面需要新鮮數據；過去版本 SW 攔截後配合 cache:'no-store' 偶爾會卡住 fetch 數十秒。
  //   不呼叫 event.respondWith 等同放行給瀏覽器原生 fetch 行為。
  if (pathname.includes('/data/') && pathname.endsWith('.json')) return;
  if (url.includes('/proxy?')) return;
  if (url.includes('_nosw=')) return;

  // HTML 主文件：永遠走網路（bypass cache）
  if (url.endsWith('.html') || event.request.mode === 'navigate') {
    event.respondWith(
      fetch(event.request, { cache: 'no-store' }).catch(() => caches.match(event.request))
    );
    return;
  }

  // fundamentals.json → 網路優先（離線才用快取）
  if (url.includes('fundamentals.json')) {
    event.respondWith(
      fetch(event.request, { cache: 'no-store' })
        .then(res => {
          if (res.ok) {
            const clone = res.clone();
            caches.open(CACHE_NAME).then(c => c.put(event.request, clone));
          }
          return res;
        })
        .catch(() => caches.match(event.request))
    );
    return;
  }

  // 其他資源 → 網路優先，失敗再用快取
  event.respondWith(
    fetch(event.request)
      .then(res => {
        if (res.ok) {
          const clone = res.clone();
          caches.open(CACHE_NAME).then(c => c.put(event.request, clone));
        }
        return res;
      })
      .catch(() => caches.match(event.request))
  );
});

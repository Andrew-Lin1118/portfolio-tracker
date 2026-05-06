/* Portfolio Tracker – Service Worker
   版本號更新會清除舊快取並重新下載 */
const CACHE_NAME = 'portfolio-tracker-v100';  // ← 2026-05-06：sticky 代碼欄底色 #1d3050 → #0f2238（更貼近卡片底色，減少視覺差）
const PRECACHE = [
  './portfolio-tracker-v13.html',
  './data/fundamentals.json',
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
//   - fundamentals.json → 快取優先（離線也能顯示舊資料）
//   - 其他資源 → 網路優先，失敗再用快取
self.addEventListener('fetch', event => {
  const url = event.request.url;

  // HTML 主文件：永遠走網路（bypass cache）
  if (url.endsWith('.html') || event.request.mode === 'navigate') {
    event.respondWith(
      fetch(event.request, { cache: 'no-store' }).catch(() => caches.match(event.request))
    );
    return;
  }

  // fundamentals.json → 快取優先（離線也能顯示舊資料）
  if (url.includes('fundamentals.json')) {
    event.respondWith(
      caches.open(CACHE_NAME).then(cache =>
        cache.match(event.request).then(cached => {
          const fresh = fetch(event.request).then(res => {
            cache.put(event.request, res.clone());
            return res;
          });
          return cached || fresh;
        })
      )
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

# 症狀觀察卡片圖片更新摘要

## 更新日期
2026-10-13

## 完成項目

### 1. 圖片資源
新增 `images/symptoms/` 目錄，包含 10 張症狀圖片：
- 睡眠差.png (430K)
- 手腳冰冷.png (452K)
- 口乾.png (521K)
- 容易疲勞.png (494K)
- 消化不良.png (447K)
- 容易水腫.png (501K)
- 皮膚敏感.png (457K)
- 容易生氣.png (586K)
- 頭暈.png (430K)
- 夜尿頻繁.png (517K)

### 2. HTML 結構更新
- 將症狀觀察區塊的 10 個卡片從 Emoji 圖示替換為 `<img>` 標籤
- 每個卡片添加 `class="has-image"` 以啟用圖片樣式
- 圖片路徑：`images/symptoms/{症狀名稱}.png`
- 支援 lazy loading 和 async decoding

### 3. CSS 樣式
症狀卡片使用現有 `.obs-card.has-image` 樣式：
- 1:1 正方形比例（aspect-ratio: 1/1）
- object-fit: contain 保持圖片比例
- 圓角 + 柔和陰影
- Hover 浮起效果
- 響應式網格：手機端 3 欄，桌面端 4 欄

### 4. Git 提交
```
13b928b style: update symptom observation icons with custom images and refine card grid
b6a2dae fix: update API_BASE to Render backend https://healthcheck-sj4i.onrender.com/api
dbf6d45 fix(phase3): add async smtp email notification with admin fallback
```

### 5. 測試驗證
- ✅ 本地服務可正常啟動
- ✅ API 路由完整（/, /api/health, /api/questionnaire/submit）
- ✅ 10 個症狀卡片全部渲染正確
- ✅ 圖片路徑無錯誤

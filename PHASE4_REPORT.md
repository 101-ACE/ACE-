# COSWAY TCM Phase 4 上線前檢核報告
## 日期：2026-11-25
## URL：https://healthcheck-bel.pages.dev/

---

## ✅ 部署狀態摘要

| 項目 | 狀態 | 詳細 |
|------|------|------|
| 前端 (pages.dev) | ✅ LIVE | MD5 `dc958956` / 186263 bytes，與本地完全一致 |
| 後端 (Render) | ✅ LIVE | API `/submit` 正常，4 組測試案例全部通過 |
| Git 推送 | ✅ OK | HEAD = origin/main = d34f994（11 commits） |
| 備份盤 | ✅ OK | `backup_legacy_20261125_044722/` 含 main.py + index.html |

---

## 📋 Dave 實測時應重點檢查的 5 項

### 1️⃣ 5 步 Wizard 完整流程
打開 https://healthcheck-bel.pages.dev/，依序選取：
- **Step 1**：選「A 易累無神」（氣虛基準）
- **Step 2**：選「A 腹部肥胖消化不良」（痰濕疊加）
- **Step 3**：選「A 面色白眼疲倦」（氣虛疊加）
- **Step 4**：選「A 舌淡白齒痕胖大瘦薄」（氣虛疊加）
- **Step 5**：選「A 立即查看預約領取禮包」

**預期結果**：
- 結果頁顯示「氣虛質」，分數約 35–50，風險等級高
- 右下出現「預約領取養生禮包及檢測」CTA 區塊（琥珀色卡片）
- CTA 文字包含 WhatsApp / LINE / Facebook 圖示

### 2️⃣ 進階細項版本切換
- 右上角看到 ⚙️ **「進階細項版本」** 按鈕（圓角 pill 樣式）
- 點擊後頁面跳轉為？advanced=1，顯示完整的 11 大觀察類別 checkboxes
- 在進階頁底部可看到「返回簡易版」連結
- 点击返回連結後回到 5 步 Wizard，URL 移除 ?advanced=1

### 3️⃣ 全流程空選 / 全正常選項
- Step 1 E、Step 2 D、Step 3 D、Step 4 D 全選「正常/紅潤/淡紅」
- Step 5 選 B「先看報告再決定」

**預期結果**：
- 結果頁顯示「平和質」，score=90，risk_level=低
- 風險指標區全空白或僅顯示正面建議
- 不會觸發 CTA 預約流程（只有「再看一次報告」）

### 4️⃣ 手機版版面驗證
- 用 Chrome DevTools 開 Mobile viewport（iPhone 14 / Pixel 5）
- 確認 5 個步驟面板正確切換、無遮擋
- Step 進度條（圓點+連線）縮小後仍清晰可讀
- 「進階細項版本」按鈕不因右對齊而超出螢幕

### 5️⃣ 結果頁 CTA 預約功能
- 在 Step 5 選 A「立即查看預約領取禮包」
- 結果頁下方 CTA 區塊應完整顯示：
  - 禮品包圖片（左側，圓角）
  - 暱稱 + WhatsApp 輸入框
  - 「立即預約」按鈕（綠色漸層）
- 填寫資料後送出，應跳出 Toast：`✅ 已預約門市禮品包！`
- 同時跳轉至 WhatsApp 聯繫畫面（web.whatsapp.com）

---

## 🔍 已知事項（非 Bug）

| 現象 | 說明 |
|------|------|
| 血瘀體質顯示為「血瘀障礙型」 | CONSTITUTION_FULL_NAMES 設計用醫學術語名稱 |
| 陽虛顯示為「陽虛水泛型」 | 同上，為正式中醫診斷名稱 |
| Render 後端冷啟動 ~30s | 免費方案睡眠後首次請求會觸發冷啟 |

---

## 📦 本次上線包含的 commits

```
d34f994 docs: Phase3 修復摘要｜計分模型+Render部署指引
edd0c8c fix: Phase3 補存 wizard_intent 至 Assessment 記錄
b4b8900 docs: 新增分析路徑指南（計分模型說明、部署路徑、緊急修復檢查點）
3e546ea fix: Phase3 修正analyze_constitution使用WIZARD_ANSWER_KEYS權重並修補Step1A平和問題
244f484 fix: Phase3 修正計分模型｜降低Step1-2權重+改用sqrt縮放避免多步合併後分數瞬間歸零
3878965 feat: Phase 3 串接 5步權重計分、結果頁與引流串接
```

## 🚨 阻塞事項

**Render 後端需手動 redeploy** 以同步最新 wizard_intent 存檔邏輯：
→ https://dashboard.render.com/service/tcm-backend/deployments → Manual Deploy → Deploy latest commit

部署完成後，所有新提交的測試用例（含 wizard_intent）將正確存庫。

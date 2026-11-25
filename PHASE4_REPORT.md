# COSWAY TCM Phase 4 上線前檢核報告
## 日期：2026-11-25
## URL：https://healthcheck-bel.pages.dev/

---

## ✅ 部署狀態摘要（Phase 4.3 更新）

| 項目 | 狀態 | 詳細 |
|------|------|------|
| 前端 (pages.dev) | ✅ LIVE | MD5 `3dc93f76` / **190,645 bytes**，與本地完全一致 |
| 後端 (Render) | ✅ OK | API 正常運行，Step 5 體重分流權重已上線，email_sent=True |
| Git 推送 | ✅ OK | HEAD = origin/main = `56f0a01`（17 commits，含 Phase 4.2+4.3） |
| 備份盤 | ✅ OK | `/…/tcm_health_assessment_backup_legacy_20261125_044722/` 完整保留 |

---

## 📋 本次上線包含的 commits

```
56f0a01 docs: Phase4.3 實測檢核清單 + 自動驗證腳本
8e0b889 docs: Phase4.2 重整報告｜5步體重分流+圖庫+計分測試結果
9d470a5 feat: Phase 4.2 重整 5步為體質分流題目+圖片輔助 | Step5改循環代謝題、新增圖庫、權重重新分配
9906303 docs: Phase4.1 修復報告｜白屏問題根治+驗證清單
0a447fe fix: Phase4.1 修復 wizard 提交後白屏 | await renderReport + 錯誤邊界
129eea1 docs: Phase4 報告更新｜補Render redeploy說明+完整commit清單
```

---

## 🔧 Phase 4.1 修復摘要（已完成）

**白屏根因**：`renderReport(data)` 未 await，導致 `showStage("stage2")` 在報告渲染完成前執行。  
**修復**：加入 `await renderReport()` + try/catch 錯誤邊界。

---

## 🆕 Phase 4.2 重整摘要

### 變更內容

| 步驟 | 原設計 | 現設計 |
|------|--------|--------|
| Step 1 | 日常能量與睡眠（5選項，可選1-2） | ✅ 維持不變，新增圖庫輔助 |
| Step 2 | 腸胃身型代謝（4選項） | ✅ 維持不變，新增圖片輔助 |
| Step 3 | 臉色眼睛嘴唇（4選項） | ✅ 維持不變，新增圖片輔助 |
| Step 4 | 舌象快速看一眼（4選項） | ✅ 維持不變，新增舌象示意圖 |
| **Step 5** | ~~跟進意願分流（預約vs先看報告）~~ | **🔄 循環代謝狀態分流（4選項）** |

### Step 5 新選項與權重映射

| 選項 | data-value | 症狀子項 | 偏頗體質權重 |
|------|-----------|---------|------------|
| A | `循環卡頓痠痛麻` | 肩頸僵硬、局部刺痛 | 氣鬱+2-3，血瘀+2-3 |
| B | `排便黏膩口氣重` | 排便黏膩、口氣重 | 濕熱+3-4，痰濕+2-3 |
| C | `心悸氣短精力透支` | 心悸、氣短、精力透支 | 氣虛+3-4，陽虛+2-3 |
| D | `循環排泄大致順暢` | （無） | 平和基準，不加分 |

### 現有圖庫使用清單

| 類別 | 路徑 | 使用位置 |
|------|------|---------|
| 症狀圖檔 | `images/symptoms/*.png` (10張) | Step 1: 容易疲勞、睡眠差、手腳冰冷、口乾 |
| 體型圖檔 | `public/images/body/*.png` (4張) | Step 2: body_fat/slouched/thin/normal |
| 面色圖檔 | `public/images/features/face/*.png` (5張) | Step 3: pale/oily/dark_spots/normal |
| 眼睛圖檔 | `public/images/eyes/*.png` (6張) | 保留供進階模式使用 |
| 唇色圖檔 | `public/images/lips/*.png` (5張) | 保留供進階模式使用 |
| 舌象圖檔 | `images/tongue/{danbai,hong,zi,fanhong}.png` | Step 4: 淡白舌/紅舌/紫舌/淡紅舌 |
| 無症狀圖檔 | `public/images/no_symptoms.png` | Step 1 E、Step 5 D |
| Lucide Icons | CDN unpkg.com/lucide@latest | Step 5 SVG 圖案替代 |

### CSS 調整

```css
/* 新加 opt-img-wrap 縮圖樣式 */
.opt-img-wrap { width:52px; height:52px; border-radius:10px; overflow:hidden; ... }
.option-card  { padding:10px 12px; min-height:72px; /* 微調以容納圖片 */ }
```

---

### 問題根因
**白屏成因**：`submitWizardResult()` 和 `submitQuestionnaire()` 中調用 `renderReport(data)` 時未使用 `await`，導致：
1. `showStage("stage2")` 在報告渲染完成前執行
2. 若 `renderReport` 內部拋出異常，整個函數 fail silently，用戶看到空白頁面且無錯誤提示

### 修復內容
```javascript
// Fix: Add await + error boundary
try {
    await renderReport(data);
} catch (renderErr) {
    console.error("[TCM] renderReport failed:", renderErr);
    alert("報告渲染失敗：" + (renderErr.message || "未知錯誤"));
    return;
}
showStage("stage2"); // Now safely after report is rendered
```

### 修復位置
- `index.html:1809` — `submitQuestionnaire()` (進階模式路徑)
- `index.html:2401` — `submitWizardResult()` (5步 Wizard 主路徑)

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
0a447fe fix: Phase4.1 修復 wizard 提交後白屏 | await renderReport + 錯誤邊界
129eea1 docs: Phase4 報告更新｜補Render redeploy說明+完整commit清單
863455d docs: Phase4 上線前檢核報告｜部署狀態+Dave實測檢查清單
d34f994 docs: Phase3 修復摘要｜計分模型+Render部署指引
edd0c8c fix: Phase3 補存 wizard_intent 至 Assessment 記錄
b4b8900 docs: 新增分析路徑指南（計分模型說明、部署路徑、緊急修復檢查點）
3e546ea fix: Phase3 修正analyze_constitution使用WIZARD_ANSWER_KEYS權重並修補Step1A平和問題
244f484 fix: Phase3 修正計分模型｜降低Step1-2權重+改用sqrt縮放避免多步合併後分數瞬間歸零
3878965 feat: Phase 3 串接 5步權重計分、結果頁與引流串接
6762f6f fix: 修復 Wizard 得分 Key 錯誤｜移除不存在的血虛體質 key
eea8462 fix: 補全進階模式表單元素與提交綁定
c6c1f3b fix: 修復進階模式表單提交邏輯
3ccb4c8 fix: 平和體質得分修正｜避免全部選健康選項時仍報高風險
```

---

## ✅ Phase 4.2 完成檢查清單

- [x] Step 5 題目從「跟進意願分流」改為「循環代謝狀態分流」
- [x] `WIZARD_ANSWER_KEYS` 新增 Step 5 四個選項權重映射
- [x] `WIZARD_SYNONYM_MAP` + `WIZARD_INLINE_WEIGHTS` 新增 7 個子症狀項
- [x] 5步驟所有選項加入對應圖庫圖片（共 24 張圖片縮圖）
- [x] CSS 新增 `.opt-img-wrap` 樣式 + 調整 `.option-card` padding/min-height
- [x] JS `wizardFinish()` 移除 Step 5 intent 邏輯，固定使用 CTA 預設值
- [x] JS `submitWizardResult()` 移除 `intent` 變數，claim-section 一律顯示
- [x] Git commit `9d470a5` 推送至 origin/main
- [x] Cloudflare Pages 自動部署完成 (190645 bytes)
- [x] 後端端到端測試：氣虛質(分38)/血瘀質(分42)/平和質(分85) 三 case 全部通過

---

## 🧪 Phase 4.2 計分測試結果（已驗證）

| 測試案例 | Step 1→5 選取 | constitution | score | risk | indicators |
|---------|--------------|-------------|-------|------|-----------|
| 氣虛質（典型+Step5疊加） | A易累無神 / A腹部肥胖 / A面色白眼 / A舌淡白 / **C心悸氣短** | 氣虛質 | 38 | 高 | 免疫機能下降, 身體恢復力減弱 |
| 血瘀體質 | B睡差嘆氣 / D無困擾 / C膚色暗沉 / C舌紫青 / **A循環卡頓** | 血瘀障礙型 | 42 | 高 | 血液循環阻力增加, 組織供氧不足 |
| 平和質（全正常） | E大致正常 / D無困擾 / D氣色紅潤 / D舌淡紅不確定 / **D順暢** | 平和質 | 85 | 低 | （空白） |

---

## 🚨 下一步行動

**Dave 實測重點**：
1. 打開 https://healthcheck-bel.pages.dev/
2. 確認每步選項右側有圖片輔助理解（Step1-5均有）
3. 完成 5 步 Wizard 流程（含新 Step 5），確認不會白屏
4. 結果頁應顯示完整報告 + 底下定 CTA 預約區塊
5. 檢查手機版版型（圖片縮圖在小螢幕上是否擠壓）
6. 右上角 ⚙️ 進階細項版本按鈕仍正常運作

如有任何問題，請查看瀏覽器控制台（F12 → Console）中的 `[TCM]` 開頭日誌。

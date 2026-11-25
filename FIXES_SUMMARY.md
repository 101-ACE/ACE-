# COSWAY TCM 階段性修復摘要 — 2026-11-25

## ✅ 已完成事項

### Phase 3 計分模型修正（3 commits）
| Commit | 說明 |
|--------|------|
| 244f484 | 降低 Step1/Step2 權重，改用 sqrt 縮放公式 |
| 3e546ea | analyze_constitution 改讀 WIZARD_ANSWER_KEYS 權重（而非舊 INLINE_WEIGHTS） |
| edd0c8c | submit 端點新增 `wizard_intent=data.wizard_intent` 存入資料庫 |

### 驗證結果
- ✅ Cloudflare Pages 前端：HTTP 200 / 186263 bytes（與本地同步）
- ✅ 後端計分測試：氣虛 combo → score=40 risk=高；平衡 combo → score=90 risk=低
- ✅ Git origin/main：最新 commit edd0c8c 已推送

## ⚠️ 阻塞項

### Render 後端未自動部署最新程式碼
目前 live Render 仍運行舊版（不含 wizard_intent 存檔）。需手動觸發 redeploy：

**操作路徑：**
1. 登入 https://dashboard.render.com
2. 進入 tcm-backend 服務頁面
3. 點擊「Manual Deploy」→ 「Deploy latest commit」

或執行：
```bash
curl -X POST "https://api.render.com/v1/services/srv-tcm-backend/deployments" \
  -H "Authorization: Bearer $RENDER_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"commitSha":"edd0c8c","forceBuild":true}'
```

## 📊 計分模型最終參數

### 核心公式
```
score = max(20, round(85 - sqrt(dominant_score) * 8))
risk = 高(score<60) / 中(score<80) / 低(≥80)
```

### 單步權重對照
| 選項 | dominant體質 | 預估score範圍 |
|------|-------------|--------------|
| 易累無神 | 氣虛 | ~65 |
| 睡差煩躁嘆氣 | 氣鬱 | ~57 |
| 怕冷手腳冰冷 | 陽虛 | ~62 |
| 口乾敏感起疹 | 陰虛/特稟 | ~69 |
| 腹部肥胖消化不良 | 痰濕 | ~62 |
| 面色白眼疲倦 | 氣虛 | ~55 |
| 出油泛紅痘黑眼圈 | 濕熱 | ~58 |
| 膚色暗沉唇甲紫暗 | 血瘀 | ~56 |

### 多步合併實測
| 組合 | constitution | score | risk |
|------|-------------|-------|------|
| Qi deficiency full | 氣虛質 | 40 | 高 |
| Damp-heat full | 濕熱質 | ~45 | 高 |
| Blood stasis full | 血瘀質 | ~43 | 高 |
| Full balanced | 平和質 | 90 | 低 |

## 📁 相關檔案
- `main.py` — 後端主程式（FastAPI + SQLModel）
- `index.html` — 前端主檔（Cloudflare Pages）
- `ANALYSIS_PATH.md` — 完整分析路徑指南

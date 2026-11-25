# COSWAY TCM 診斷系統 · 分析路徑指南 v1.0

## 架構總覽
```
前端 Cloudflare Pages  →  http://healthcheck-bel.pages.dev/
後端 Render           →  https://healthcheck-sj4i.onrender.com/api/
GitHub               →  https://github.com/101-ACE/ACE-
Google Sheets         →  gid=1552781706（客戶記錄）
WhatsApp 推播        →  UltraMsg API（需配置 token）
```

## 核心檔案結構
```
tcm_health_assessment/
├── index.html          ← 前端主檔（Cloudflare Pages 部署）
├── main.py             ← FastAPI 後端（Render 部署）
├── css/style.css       ← 樣式表
├── js/app.js           ← 前端互動邏輯
└── images/             ← PNG 圖片資源
```

## 計分模型說明

### Wizard 5步簡化流程（預設）
| 步驟 | 選項 | 映射 |
|------|------|------|
| Step1 | A 易累無神 | 氣虛(+3痰濕+1血瘀) |
|       | B 睡差煩躁嘆氣 | 氣鬱(+3) |
|       | C 怕冷手腳冰冷 | 陽虛(+4) |
|       | D 口乾敏感起疹 | 陰虛/特稟(+3) |
| Step2 | A 腹部肥胖 | 痰濕(+3) |
|       | B 容易水腫 | 痰濕+陽虛(+3/+2) |
| Step3 | A 面色白眼疲倦 | 氣虛(+5) |
|       | B 出油泛紅痘 | 濕熱(+4) |
|       | C 膚色暗沉唇甲紫暗 | 血瘀(+5) |
| Step4 | A 舌淡白齒痕 | 氣虛(+4) |
|       | B 舌紅黃苔 | 濕熱(+4) |
|       | C 舌紫青 | 血瘀(+5) |
| Step5 | 跟進意願 A/B | wizard_intent 存檔 |

### 計分公式
- 各症狀對應九大體質權重（OBSERVATION_CONSTITUTION_MAP）
- WIZARD_ANSWER_KEYS 中直接定義 per-key 權重（優先於舊 INLINE_WEIGHTS）
- Score = max(20, round(85 - sqrt(dom_score) * 8))
- Risk: score<60 高 / <80 中 / ≥80 低

### 權重調整歷史
| 日期 | 變更 | 原因 |
|------|------|------|
| 2026-11-25a | Step1-2權重降低、改用sqrt縮放 | 4步合併時dominant過於集中→分數瞬間歸零 |
| 2026-11-25b | analyze_constitution使用WIZARD_ANSWER_KEYS權重 | 舊INLINE_WEIGHTS固定值干擾權重調整 |

## 部署路徑
| 環境 | URL | 部署方式 |
|------|-----|---------|
| 前端 | https://healthcheck-bel.pages.dev/ | GitHub push → CF Pages自動觸發 |
| 後端 | https://healthcheck-sj4i.onrender.com/ | Render Dashboard 手動 redeploy |

## 緊急修復檢查點
1. `git pull origin main` → 本地同步
2. `python3 main.py` → 驗證無語法錯誤
3. `curl POST /api/questionnaire/submit` → 測試提交端點
4. `git push origin main` → 觸發CF Pages
5. Render Dashboard → Manual deploy
6. 驗證: `https://healthcheck-bel.pages.dev/` HTTP 200

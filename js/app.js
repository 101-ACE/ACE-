// ===== 前端配置 =====
// 生產環境請修改此處為實際後端 API 網址
// 範例：const API_BASE = "https://api.your-domain.com/api";
// 若前後端同域，可改為相对路徑：const API_BASE = "/api";
const API_BASE = "http://localhost:8000/api";

const SCAN_MESSAGES = [
    "正在初始化 AI 分析引擎...",
    "正在分析您的九種體質...",
    "計算經絡氣血狀態...",
    "評估健康風險指標...",
    "生成個人化調理建議...",
];

function getQueryParam(name) {
    const params = new URLSearchParams(window.location.search);
    return params.get(name);
}

function $(id) { return document.getElementById(id); }

function showStage(id) {
    document.querySelectorAll(".stage").forEach(s => s.classList.remove("active"));
    $(id).classList.add("active");
}

function getSymptoms() {
    return Array.from(document.querySelectorAll("#symptoms input:checked")).map(el => el.value);
}

function validateForm() {
    const fields = ["name", "phone", "gender", "age_group"];
    for (const f of fields) {
        if (!$(f).value.trim()) {
            alert("請填寫所有必填欄位");
            $(f).focus();
            return false;
        }
    }
    const symptoms = getSymptoms();
    if (symptoms.length === 0) {
        alert("請至少選擇一個體徵觀察");
        return false;
    }
    return true;
}

function autoFillReferralCode() {
    const ref = getQueryParam("ref");
    if (ref && $("referral_code")) {
        $("referral_code").value = ref;
    }
}

function getGaugeColor(score) {
    if (score < 60) return "#e74c3c";
    if (score < 80) return "#f39c12";
    return "#27ae60";
}

function getRiskClass(risk) {
    if (risk === "中") return "medium";
    if (risk === "低") return "low";
    return "";
}

function renderGauge(score) {
    const circumference = 2 * Math.PI * 70;
    const percent = Math.max(0, Math.min(100, score));
    const offset = circumference - (percent / 100) * circumference;
    const color = getGaugeColor(score);
    return `
        <div class="gauge-wrap">
            <svg viewBox="0 0 160 160" width="100%" height="100%">
                <circle class="gauge-bg" cx="80" cy="80" r="70"></circle>
                <circle class="gauge-fill" cx="80" cy="80" r="70"
                    stroke="${color}"
                    stroke-dasharray="${circumference}"
                    stroke-dashoffset="${offset}"
                    transform="rotate(-90 80 80)"></circle>
            </svg>
            <div class="gauge-text">
                <div class="gauge-number" style="color:${color}">${score}</div>
                <div class="gauge-label">健康警示分</div>
            </div>
        </div>
    `;
}

function runScanAnimation() {
    return new Promise((resolve) => {
        showStage("scanning");
        const scanText = $("scan-text");
        const scanBarFill = $("scan-bar-fill");
        let index = 0;
        const interval = setInterval(() => {
            if (index < SCAN_MESSAGES.length) {
                scanText.textContent = SCAN_MESSAGES[index];
                scanBarFill.style.width = `${((index + 1) / SCAN_MESSAGES.length) * 100}%`;
                index++;
            } else {
                clearInterval(interval);
                setTimeout(resolve, 400);
            }
        }, 600);
    });
}

async function submitQuestionnaire(e) {
    e.preventDefault();
    if (!validateForm()) return;

    const payload = {
        name: $("name").value.trim(),
        phone: $("phone").value.trim(),
        gender: $("gender").value,
        age_group: $("age_group").value,
        referral_code: $("referral_code").value.trim() || null,
        answers: { symptoms: getSymptoms() },
    };

    const btn = e.target.querySelector("button[type='submit']");
    btn.disabled = true;
    btn.textContent = "分析中...";

    try {
        await runScanAnimation();
        const resp = await fetch(`${API_BASE}/questionnaire/submit`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
        });
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.detail || "提交失敗");
        renderReport(data);
        showStage("stage2");
        setTimeout(() => {
            $("report-content").scrollIntoView({ behavior: "smooth", block: "start" });
        }, 100);
    } catch (err) {
        alert("提交失敗：" + err.message);
    } finally {
        btn.disabled = false;
        btn.textContent = "開始 AI 體質分析";
    }
}

async function renderReport(data) {
    const report = data.report;
    const score = report.score;
    const risk = report.risk_level;
    const container = $("report-content");
    const riskClass = getRiskClass(risk);
    const riskLabel = risk === "高" ? "潛在風險等級：高" : (risk === "中" ? "潛在風險等級：中" : "潛在風險等級：低");
    container.innerHTML = `
        <div class="score-dashboard">
            ${renderGauge(score)}
            <div class="risk-badge ${riskClass}">${riskLabel}</div>
            <div style="margin-top:14px; font-size:1rem; color:#4a7c6a;">體質判定：<strong style="color:#1a4a3a;">${report.constitution}</strong></div>
        </div>
        <div class="warning-block">
            <h3>⚠️ 健康風險關注指標</h3>
            ${(report.risk_indicators || []).map(i => `
                <div class="warning-item">
                    <span class="warn-icon">●</span>
                    <span>${i}</span>
                </div>
            `).join("")}
            <div style="margin-top:14px;">
                <div class="risk-item" style="margin:8px 0;">
                    <div class="risk-text">血管彈性指數</div>
                    <div class="risk-meter"><div class="risk-meter-fill ${riskClass}" style="width:${Math.min(100, Math.max(20, 100 - score + 10))}%"></div></div>
                </div>
                <div class="risk-item" style="margin:8px 0;">
                    <div class="risk-text">血液粘稠度預警</div>
                    <div class="risk-meter"><div class="risk-meter-fill ${riskClass}" style="width:${Math.min(100, Math.max(20, 100 - score + 5))}%"></div></div>
                </div>
                <div class="risk-item" style="margin:8px 0;">
                    <div class="risk-text">肝臟代謝負擔</div>
                    <div class="risk-meter"><div class="risk-meter-fill ${riskClass}" style="width:${Math.min(100, Math.max(20, 100 - score))}%"></div></div>
                </div>
            </div>
        </div>
    `;
    const recContainer = $("recommendations");
    fetch(`${API_BASE}/report/${data.assessment_id}`)
        .then(r => r.json())
        .then(reportData => {
            recContainer.innerHTML = `
                <div class="recommendations">
                    <h3>🍵 個人化調理建議</h3>
                    ${reportData.recommendations.map(r => `<div class="recommendation-item">• ${r}</div>`).join("")}
                </div>
            `;
        });
    window.__assessmentId = data.assessment_id;
}

function showPopup() {
    const popup = $("popup");
    if (popup) popup.classList.add("active");
}

function hidePopup() {
    const popup = $("popup");
    if (popup) popup.classList.remove("active");
}

async function confirmBooking() {
    const id = window.__assessmentId;
    if (!id) return alert("找不到檢測記錄");
    try {
        const resp = await fetch(`${API_BASE}/booking/confirm`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ assessment_id: id, confirmed: true }),
        });
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.detail || "預約失敗");
        showPopup();
    } catch (err) {
        alert("預約失敗：" + err.message);
    }
}

$("questionnaire-form").addEventListener("submit", submitQuestionnaire);
$("confirm-booking").addEventListener("click", confirmBooking);
$("popup-close").addEventListener("click", hidePopup);

// 啟動時自動帶入 ?ref= 檢測人員編號
autoFillReferralCode();

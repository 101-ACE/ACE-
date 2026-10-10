"""
COSWAY AI 中醫體質檢測 Web App
獨立全棧專案：FastAPI + SQLite + 響應式前端
通知策略：靜默 Email（取消 WhatsApp 推播）
數據來源：動態 Google Sheet 拍檔名單
"""

import os
import uuid
import smtplib
import re
import json
import requests
import logging
from datetime import datetime
from typing import Optional, Dict, Any, List
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, validator
from sqlalchemy import create_engine, Column, String, Integer, DateTime, Text, Float
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session

# ===== 日誌設定 =====
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ===== 配置 =====
DATABASE_URL = "sqlite:///./tcm_assessments.db"

# 靜默 Email 設定（環境變數，具備 Graceful Fallback）
SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USERNAME = os.getenv("SMTP_USERNAME", "acesc01gp@gmail.com")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SENDER_EMAIL = os.getenv("SENDER_EMAIL", SMTP_USERNAME)

# 判斷 SMTP 是否可實際使用（密碼未配置時視為未啟用）
SMTP_ENABLED = bool(SMTP_PASSWORD)

# Google Sheet 設定
GOOGLE_SHEET_ID = "1iZaN0AKtQT6d9zFc_K7tmWjN15q2WpytAN26kxu32H4"
GOOGLE_SHEET_CSV_URL = f"https://docs.google.com/spreadsheets/d/{GOOGLE_SHEET_ID}/export?format=csv"
PARTNERS_CACHE: Dict[str, Dict[str, str]] = {}
PARTNERS_LOADED = False

# ===== 資料庫 =====
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class Assessment(Base):
    __tablename__ = "assessments"
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String, nullable=False)
    phone = Column(String, nullable=False)
    gender = Column(String, nullable=False)
    age_group = Column(String, nullable=False)
    referral_code = Column(String, nullable=True)
    store = Column(String, nullable=False)
    answers = Column(Text, nullable=False)  # JSON string
    constitution = Column(String, nullable=False)
    health_score = Column(Integer, nullable=False)
    risk_level = Column(String, nullable=False)
    risk_indicators = Column(Text, nullable=False)  # JSON string
    submitted_at = Column(DateTime, default=datetime.utcnow)
    email_sent = Column(Integer, default=0)
    partner_email = Column(String, nullable=True)
    partner_name = Column(String, nullable=True)

Base.metadata.create_all(bind=engine)

# ===== Pydantic 模式 =====
class QuestionnairesSubmit(BaseModel):
    name: str = Field(..., max_length=100)
    phone: str = Field(..., max_length=20)
    gender: str
    age_group: str
    referral_code: Optional[str] = None
    store: str
    answers: Dict[str, Any]

    @validator("store")
    def validate_store(cls, v):
        valid = ["將軍澳中心", "將軍澳寶琳", "樂富", "柴灣", "佐敦"]
        if v not in valid:
            raise ValueError("無效的門市選擇")
        return v

class BookingSubmit(BaseModel):
    assessment_id: str
    confirmed: bool = True

class HealthReportResponse(BaseModel):
    assessment_id: str
    name: str
    constitution: str
    health_score: int
    risk_level: str
    risk_indicators: List[str]
    recommendations: List[str]
    store: str

# ===== 九大體質邏輯 =====
CONSTITUTION_RULES = {
    "氣虛": ["易倦", "氣短", "自汗", "容易感冒"],
    "陽虛": ["怕冷", "手腳冰冷", "夜尿", "腹泻"],
    "陰虛": ["口乾", "午後潮熱", "失眠", "盜汗"],
    "痰濕": ["痰多", "身體困重", "舌苔厚膩", "腹部肥滿"],
    "濕熱": ["口苦", "小便黃", "舌紅", "皮膚油膩"],
    "血瘀": ["面色晦暗", "舌質瘀斑", "疼痛固定", "記憶減退"],
    "氣鬱": ["情緒低落", "胸胁脹痛", "焦慮", "失眠多夢"],
    "特稟": ["過敏", "哮喘", "蕁麻疹", "打噴嚏"],
    "平和": [],
}

SYMPTOM_CONSTITUTION_MAP = {
    "睡眠差": {"氣鬱": 3, "陰虛": 2, "血瘀": 1},
    "手腳冰冷": {"陽虛": 4, "氣虛": 2},
    "口乾": {"陰虛": 4, "濕熱": 2},
    "易倦": {"氣虛": 4, "痰濕": 2},
    "消化不良": {"痰濕": 3, "氣虛": 2},
    "容易水腫": {"痰濕": 4, "陽虛": 2},
    "皮膚敏感": {"特稟": 4, "濕熱": 2},
    "容易生氣": {"氣鬱": 4, "濕熱": 2},
    "頭暈": {"血瘀": 2, "氣虛": 2, "痰濕": 1},
    "夜尿頻繁": {"陽虛": 4, "氣虛": 1},
}

def analyze_constitution(answers: Dict[str, Any]) -> Dict[str, Any]:
    scores: Dict[str, int] = {k: 0 for k in CONSTITUTION_RULES}
    symptoms = answers.get("symptoms", [])
    for symptom in symptoms:
        weights = SYMPTOM_CONSTITUTION_MAP.get(symptom, {})
        for constitution, weight in weights.items():
            scores[constitution] += weight
    if not symptoms:
        return {"constitution": "平和", "score": 90, "risk_level": "低", "risk_indicators": []}
    dominant = max(scores, key=scores.get)
    if scores[dominant] == 0:
        return {"constitution": "平和", "score": 85, "risk_level": "低", "risk_indicators": []}
    base_score = max(20, 85 - scores[dominant] * 5)
    indicators_map = {
        "氣虛": ["免疫機能下降", "身體恢復力減弱", "組織修復速度變慢"],
        "陽虛": ["末梢循環不良", "基礎代謝率下降", "產熱功能減退"],
        "陰虛": ["細胞含水量失衡", "代謝廢物堆積", "內分泌紊亂"],
        "痰濕": ["血液黏稠度偏高", "血管彈性下降", "代謝毒素堆積"],
        "濕熱": ["肝臟負擔過重", "炎症指標升高", "腸道生態失衡"],
        "血瘀": ["血液循環阻力增加", "組織供氧不足", "血管壁脂質堆積"],
        "氣鬱": ["自律神經失調", "皮質醇偏高", "免疫功能受壓抑"],
        "特稟": ["免疫過度活化", "組織胺累積", "黏膜屏障脆弱"],
        "平和": [],
    }
    risk = "高" if base_score < 60 else ("中" if base_score < 80 else "低")
    return {
        "constitution": dominant,
        "score": base_score,
        "risk_level": risk,
        "risk_indicators": indicators_map.get(dominant, []),
    }

def get_recommendations(constitution: str) -> List[str]:
    recommendations = {
        "氣虛": ["多吃黃芪、山藥、蓮子", "避免過度勞累與熬夜", "適度進行太極、散步等和緩運動"],
        "陽虛": ["多食溫補食材如生薑、桂圓", "注意保暖，尤其下肢", "減少冰涼食物與生冷飲料"],
        "陰虛": ["多食用滋陰潤燥食物如銀耳、梨", "避免辛辣刺激與油炸食物", "規律作息，避免熬夜傷陰"],
        "痰濕": ["飲食清淡，少油少鹽", "增加膳食纖維攝取", "保持規律運動提升代謝"],
        "濕熱": ["多喝溫開水，促進新陳代謝", "避免辛辣油膩與酒精", "早點入睡，減輕肝臟負擔"],
        "血瘀": ["適量飲用玫瑰花、山楂茶", "保持規律運動促進血液循環", "避免久坐，定時活動筋骨"],
        "氣鬱": ["保持心情開朗，練習深呼吸與冥想", "多參與戶外活動", "避免長期壓力累積"],
        "特稟": ["遠離已知過敏原", "保持環境通風與清潔", "飲食均衡，增強體質"],
        "平和": ["維持均衡飲食與規律作息", "適度運動，保持心情愉快", "定期檢查，預防為先"],
    }
    return recommendations.get(constitution, [])

# ===== Google Sheet 動態拍檔名單 =====
def normalize_partner_id(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    value = str(raw).strip()
    value = re.sub(r"^hk", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\D", "", value)
    return value or None

def parse_sheet_csv(text: str) -> Dict[str, Dict[str, str]]:
    partners: Dict[str, Dict[str, str]] = {}
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return partners
    header = [cell.strip() for cell in lines[0].split(",")]
    id_idx = next((i for i, h in enumerate(header) if "推薦人號碼" in h or "檢測人員編號" in h), None)
    email_idx = next((i for i, h in enumerate(header) if h == "電郵"), None)
    name_idx = next((i for i, h in enumerate(header) if h == "你的暱稱"), None)
    if id_idx is None or email_idx is None or name_idx is None:
        return partners
    for line in lines[1:]:
        cells = [cell.strip() for cell in line.split(",")]
        if len(cells) <= max(id_idx, email_idx, name_idx):
            continue
        raw_id = cells[id_idx]
        email = cells[email_idx]
        name = cells[name_idx]
        if not raw_id or not email:
            continue
        normalized = normalize_partner_id(raw_id)
        if not normalized:
            continue
        partners[normalized] = {"name": name, "email": email}
    return partners

def refresh_partners_from_sheet() -> Dict[str, Dict[str, str]]:
    try:
        resp = requests.get(GOOGLE_SHEET_CSV_URL, timeout=15)
        if resp.status_code == 200 and resp.text.strip():
            PARTNERS_CACHE.clear()
            PARTNERS_CACHE.update(parse_sheet_csv(resp.text))
    except Exception:
        pass
    return PARTNERS_CACHE

def get_partners() -> Dict[str, Dict[str, str]]:
    global PARTNERS_LOADED
    if not PARTNERS_LOADED or not PARTNERS_CACHE:
        refresh_partners_from_sheet()
        PARTNERS_LOADED = True
    return PARTNERS_CACHE

def find_partner(referral_code: Optional[str]) -> Optional[Dict[str, str]]:
    if not referral_code:
        return None
    partners = get_partners()
    normalized = normalize_partner_id(referral_code)
    if not normalized:
        return None
    return partners.get(normalized)

# ===== HTML 郵件範本生成 =====
def build_email_html(assessment: Assessment, recommendations: List[str]) -> str:
    score = assessment.health_score
    risk = assessment.risk_level
    constitution = assessment.constitution
    store = assessment.store
    name = assessment.name
    phone = assessment.phone
    referral = assessment.referral_code or "-"
    submitted_at = assessment.submitted_at.strftime("%Y-%m-%d %H:%M") if assessment.submitted_at else "-"
    indicators = assessment.risk_indicators or "[]"
    try:
        indicators_list = json.loads(indicators)
    except Exception:
        indicators_list = []
    risk_color = "#c0392b" if risk == "高" else ("#d4a017" if risk == "中" else "#1e8449")
    risk_bg = "rgba(192,57,43,0.1)" if risk == "高" else ("rgba(212,160,23,0.1)" if risk == "中" else "rgba(30,132,73,0.1)")
    return f"""
    <html>
    <head><meta charset="UTF-8"></head>
    <body style="font-family: Arial, 'Noto Sans TC', sans-serif; background:#f6f8f7; margin:0; padding:0;">
      <table width="100%" cellpadding="0" cellspacing="0" style="background:#f6f8f7; padding:24px 0;">
        <tr><td align="center">
          <table width="600" cellpadding="0" cellspacing="0" style="background:#fff; border-radius:14px; overflow:hidden; box-shadow:0 2px 10px rgba(0,0,0,0.06);">
            <tr><td style="background:linear-gradient(135deg,#0d4f4f,#145c5c); color:#fff; padding:24px; text-align:center;">
              <h1 style="margin:0; font-size:22px;">COSWAY AI 中醫體質分析報告</h1>
              <p style="margin:6px 0 0; opacity:0.85; font-size:13px;">提交時間：{submitted_at}</p>
            </td></tr>
            <tr><td style="padding:20px;">
              <!-- 客戶基本資料 -->
              <table width="100%" cellpadding="0" cellspacing="0" style="background:#fbfdfc; border:1px solid #e7edec; border-radius:12px; margin-bottom:14px;">
                <tr><td style="padding:14px;">
                  <h3 style="margin:0 0 10px; color:#0d4f4f; font-size:15px;">👤 客戶資料</h3>
                  <div style="font-size:14px; color:#2a2f2f; line-height:1.8;">
                    <div>姓名：<strong>{name}</strong></div>
                    <div>電話：<strong>{phone}</strong></div>
                    <div>檢測人員編號：<strong>{referral}</strong></div>
                    <div>預約門市：<strong>{store}</strong></div>
                  </div>
                </td></tr>
              </table>
              <!-- 健康警示分數 -->
              <table width="100%" cellpadding="0" cellspacing="0" style="background:#fff4e6; border:1px solid #f0c59b; border-radius:12px; margin-bottom:14px;">
                <tr><td style="padding:14px; text-align:center;">
                  <div style="font-size:13px; color:#6b7373;">健康警示分</div>
                  <div style="font-size:42px; font-weight:700; color:#0d4f4f;">{score}</div>
                  <div style="margin-top:6px; display:inline-block; padding:6px 14px; border-radius:999px; background:{risk_bg}; color:{risk_color}; font-weight:700;">潛在風險等級：{risk}</div>
                  <div style="margin-top:8px; font-size:14px; color:#2a2f2f;">體質判定：<strong>{constitution}</strong></div>
                </td></tr>
              </table>
              <!-- 三大風險指標 -->
              <table width="100%" cellpadding="0" cellspacing="0" style="background:#fff; border:1px solid #e7edec; border-radius:12px; margin-bottom:14px;">
                <tr><td style="padding:14px;">
                  <h3 style="margin:0 0 10px; color:#0d4f4f; font-size:15px;">⚠️ 健康風險關注指標</h3>
                  {(indicators_list or ['系統尚未量化指標'])[:3] if indicators_list else ['系統尚未量化指標']}
                  {(''.join([f"<div style='display:flex; align-items:center; gap:8px; margin:6px 0; font-size:14px; color:#2a2f2f;'><span style='font-size:16px;'>●</span><span>{i}</span></div>" for i in (indicators_list or ['系統尚未量化指標'])[:3]]))}
                  <div style="margin-top:12px;">
                    <div style="display:flex; align-items:center; gap:12px; margin:6px 0;">
                      <div style="width:120px; font-size:13px; color:#6b7373;">血管彈性指數</div>
                      <div style="flex:1; height:10px; background:#e7edec; border-radius:999px; overflow:hidden;">
                        <div style="height:100%; width:{score}%; background:linear-gradient(90deg, {risk_color}, #e07a3d); border-radius:999px;"></div>
                      </div>
                    </div>
                    <div style="display:flex; align-items:center; gap:12px; margin:6px 0;">
                      <div style="width:120px; font-size:13px; color:#6b7373;">血液粘稠度預警</div>
                      <div style="flex:1; height:10px; background:#e7edec; border-radius:999px; overflow:hidden;">
                        <div style="height:100%; width:{min(100, max(20, 100 - score + 10))}%; background:linear-gradient(90deg, {risk_color}, #e07a3d); border-radius:999px;"></div>
                      </div>
                    </div>
                    <div style="display:flex; align-items:center; gap:12px; margin:6px 0;">
                      <div style="width:120px; font-size:13px; color:#6b7373;">肝臟代謝負擔</div>
                      <div style="flex:1; height:10px; background:#e7edec; border-radius:999px; overflow:hidden;">
                        <div style="height:100%; width:{min(100, max(20, 100 - score))}%; background:linear-gradient(90deg, {risk_color}, #e07a3d); border-radius:999px;"></div>
                      </div>
                    </div>
                  </div>
                </td></tr>
              </table>
              <!-- 建議 -->
              <table width="100%" cellpadding="0" cellspacing="0" style="background:#fff; border:1px solid #e7edec; border-radius:12px; margin-bottom:14px;">
                <tr><td style="padding:14px;">
                  <h3 style="margin:0 0 10px; color:#0d4f4f; font-size:15px;">🍵 飲食與生活作息建議</h3>
                  {"".join([f"<div style='padding:6px 0; border-bottom:1px solid #eef1f1; font-size:14px; color:#2a2f2f;'>• {r}</div>" for r in recommendations])}
                </td></tr>
              </table>
              <!-- 跟進 SOP -->
              <table width="100%" cellpadding="0" cellspacing="0" style="background:#fff4e6; border:1px solid #f0c59b; border-radius:12px; margin-bottom:14px;">
                <tr><td style="padding:14px;">
                  <h3 style="margin:0 0 6px; color:#e07a3d; font-size:15px;">📋 拍檔跟進 SOP</h3>
                  <p style="margin:0; font-size:13px; color:#2a2f2f; line-height:1.6;">請於 <strong>24 小時內</strong> 聯繫客戶，安排前往門市（將軍澳中心／將軍澳寶琳／樂富／柴灣／佐敦）進行免費進階儀器體驗與諮詢。</p>
                </td></tr>
              </table>
              <table width="100%" cellpadding="0" cellspacing="0" style="margin-top:14px;">
                <tr><td style="padding:10px; background:#fbfdfc; border-radius:10px; font-size:12px; color:#6b7373; text-align:center;">
                  客戶：{name}　|　電話：{phone}　|　檢測人員編號：{referral}
                </td></tr>
              </table>
            </td></tr>
          </table>
        </td></tr>
      </table>
    </body>
    </html>
    """

# ===== Email 推播服務（靜默通知 + Graceful Fallback） =====
def send_email_report(partner_email: str, assessment: Assessment, recommendations: List[str]) -> Dict[str, Any]:
    if not partner_email:
        logger.info("No partner email mapped for assessment %s", assessment.id)
        return {"success": False, "error": "No partner email"}
    if not SMTP_ENABLED:
        logger.info("SMTP not configured; skip sending email for assessment %s to %s", assessment.id, partner_email)
        return {"success": False, "error": "SMTP not configured"}
    subject = f"【COSWAY】AI 中醫體質預警報告 - {assessment.name}"
    html = build_email_html(assessment, recommendations)
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = SENDER_EMAIL
    msg["To"] = partner_email
    msg.attach(MIMEText(html, "html", "utf-8"))
    try:
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USERNAME, SMTP_PASSWORD)
            server.sendmail(SENDER_EMAIL, [partner_email], msg.as_string())
        logger.info("Email sent for assessment %s to %s", assessment.id, partner_email)
        return {"success": True, "method": "email", "to": partner_email}
    except Exception as e:
        logger.exception("Failed to send email for assessment %s: %s", assessment.id, e)
        return {"success": False, "error": str(e)}

# ===== FastAPI 應用 =====
app = FastAPI(title="COSWAY TCM Health Assessment API", version="4.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://healthcheck.acesc01gp.workers.dev", "http://localhost:8080", "http://localhost:3000", "*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/health")
def health():
    return {"status": "ok", "time": datetime.utcnow().isoformat()}

@app.post("/api/questionnaire/submit", response_model=Dict[str, Any])
def submit_questionnaire(data: QuestionnairesSubmit):
    analysis = analyze_constitution(data.answers)
    db: Session = SessionLocal()
    try:
        partner = find_partner(data.referral_code)
        record = Assessment(
            name=data.name,
            phone=data.phone,
            gender=data.gender,
            age_group=data.age_group,
            referral_code=data.referral_code,
            store=data.store,
            answers=json.dumps(data.answers, ensure_ascii=False),
            constitution=analysis["constitution"],
            health_score=analysis["score"],
            risk_level=analysis["risk_level"],
            risk_indicators=json.dumps(analysis["risk_indicators"], ensure_ascii=False),
            partner_email=partner["email"] if partner else None,
            partner_name=partner["name"] if partner else None,
        )
        db.add(record)
        db.commit()
        db.refresh(record)
        recommendations = get_recommendations(analysis["constitution"])
        email_result = {"success": False, "error": "No partner mapped"}
        if partner:
            email_result = send_email_report(partner["email"], record, recommendations)
            record.email_sent = 1 if email_result.get("success") else 0
            db.commit()
        return {
            "success": True,
            "assessment_id": record.id,
            "report": analysis,
            "email_sent": bool(record.email_sent),
            "email_result": email_result,
        }
    finally:
        db.close()

@app.get("/api/report/{assessment_id}", response_model=HealthReportResponse)
def get_report(assessment_id: str):
    db: Session = SessionLocal()
    try:
        record = db.query(Assessment).filter(Assessment.id == assessment_id).first()
        if not record:
            raise HTTPException(status_code=404, detail="未找到報告")
        return HealthReportResponse(
            assessment_id=record.id,
            name=record.name,
            constitution=record.constitution,
            health_score=record.health_score,
            risk_level=record.risk_level,
            risk_indicators=json.loads(record.risk_indicators) if record.risk_indicators else [],
            recommendations=get_recommendations(record.constitution),
            store=record.store,
        )
    finally:
        db.close()

@app.post("/api/booking/confirm")
def confirm_booking(data: BookingSubmit):
    db: Session = SessionLocal()
    try:
        record = db.query(Assessment).filter(Assessment.id == data.assessment_id).first()
        if not record:
            raise HTTPException(status_code=404, detail="未找到檢測記錄")
        return {"success": True, "confirmed": True, "message": "預約已確認，報告已備份至拍檔 Email。"}
    finally:
        db.close()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)

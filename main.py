"""
COSWAY AI 中醫體質檢測 Web App
獨立全棧專案：FastAPI + SQLite + 響應式前端
通知策略：靜默 Email（取消 WhatsApp 推播）
數據來源：動態 Google Sheet 拍檔名單
Google Sheets 客戶記錄：自動寫入「客戶記錄」工作表
"""

import os
import uuid
import smtplib
import re
import json
import requests
import logging
import base64
from datetime import datetime
from typing import Optional, Dict, Any, List
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sqlalchemy import create_engine, Column, String, Integer, DateTime, Text, Float
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session

# ===== 日誌設定 =====
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ===== 配置 =====
DATABASE_URL = "sqlite:///./tcm_assessments.db"

# ===== 靜默 Email 設定（環境變數，具備 Graceful Fallback） =====
SMTP_SERVER = os.getenv("SMTP_HOST", os.getenv("SMTP_SERVER", "smtp.gmail.com"))
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USERNAME = os.getenv("SMTP_USER", os.getenv("SMTP_USERNAME", "acesc01gp@gmail.com"))
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SENDER_EMAIL = os.getenv("SENDER_EMAIL", SMTP_USERNAME)

# 管理員備用電郵
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", SMTP_USERNAME)

# 判斷 SMTP 是否可實際使用（密碼未配置時視為未啟用）
SMTP_ENABLED = bool(SMTP_PASSWORD)

# Google Sheet 設定 - 拍檔名單
GOOGLE_SHEET_ID = "1iZaN0AKtQT6d9zFc_K7tmWjN15q2WpytAN26kxu32H4"
GOOGLE_SHEET_CSV_URL = f"https://docs.google.com/spreadsheets/d/{GOOGLE_SHEET_ID}/export?format=csv"

# Google Sheet 設定 - 客戶記錄 (自動寫入，使用同一份 Sheet 的不同分頁)
GOOGLE_CUSTOMER_SHEET_ID = "1iZaN0AKtQT6d9zFc_K7tmWjN15q2WpytAN26kxu32H4"
GOOGLE_CUSTOMER_SHEET_NAME = "客戶記錄"
GOOGLE_CREDENTIALS_FILE = os.getenv("GOOGLE_CREDENTIALS", "credentials.json")
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")
PARTNERS_CACHE: Dict[str, Dict[str, str]] = {}
PARTNERS_LOADED = False
PARTNERS_REFRESHED_AT: float = 0.0
REFRESH_INTERVAL_SECONDS = 300  # 5 分鐘快取

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
    answers = Column(Text, nullable=False)  # JSON string
    constitution = Column(String, nullable=False)
    health_score = Column(Integer, nullable=False)
    risk_level = Column(String, nullable=False)
    risk_indicators = Column(Text, nullable=False)  # JSON string
    submitted_at = Column(DateTime, default=datetime.utcnow)
    email_sent = Column(Integer, default=0)
    partner_email = Column(String, nullable=True)
    partner_name = Column(String, nullable=True)
    contacted = Column(Integer, default=0)  # 1 = 已聯絡檢測員

Base.metadata.create_all(bind=engine)

# ===== 資料庫遷移：確保現有資料庫包含所有欄位 =====
def migrate_database():
    """為現有 SQLite 資料庫新增缺失的欄位（不破壞既有資料）"""
    import sqlite3
    conn = sqlite3.connect(DATABASE_URL.replace("sqlite:///", ""))
    cursor = conn.cursor()
    try:
        # 檢查 assesseds 表有哪些欄位
        cursor.execute("PRAGMA table_info(assessments)")
        existing_cols = {row[1] for row in cursor.fetchall()}
        needed_cols = {
            "email_sent": "INTEGER DEFAULT 0",
            "partner_email": "TEXT",
            "partner_name": "TEXT",
            "contacted": "INTEGER DEFAULT 0",
        }
        for col, definition in needed_cols.items():
            if col not in existing_cols:
                cursor.execute(f"ALTER TABLE assessments ADD COLUMN {col} {definition}")
                logger.info("[DB] Added missing column: %s", col)
        conn.commit()
        logger.info("[DB] Database migration complete")
    except Exception as e:
        logger.warning("[DB] Migration skipped (may already be up-to-date): %s", e)
    finally:
        conn.close()

migrate_database()

# ===== Pydantic 模式 =====
class QuestionnairesSubmit(BaseModel):
    name: str = Field(..., max_length=100)
    phone: str = Field(..., max_length=20)
    gender: str
    age_group: str
    referral_code: Optional[str] = None
    answers: Dict[str, Any]

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

# 完整觀察值→體質映射（支援所有前端checkbox value格式，僅使用9大標準體質）
OBSERVATION_CONSTITUTION_MAP = {
    # ===== 症狀觀察 =====
    "睡眠差": {"氣鬱": 3, "陰虛": 2, "血瘀": 1},
    "症狀:睡眠差": {"氣鬱": 3, "陰虛": 2, "血瘀": 1},
    "手腳冰冷": {"陽虛": 4, "氣虛": 2},
    "症狀:手腳冰冷": {"陽虛": 4, "氣虛": 2},
    "口乾": {"陰虛": 4, "濕熱": 2},
    "症狀:口乾": {"陰虛": 4, "濕熱": 2},
    "易倦": {"氣虛": 4, "痰濕": 2},
    "症狀:易倦": {"氣虛": 4, "痰濕": 2},
    "消化不良": {"痰濕": 3, "氣虛": 2},
    "症狀:消化不良": {"痰濕": 3, "氣虛": 2},
    "容易水腫": {"痰濕": 4, "陽虛": 2},
    "症狀:容易水腫": {"痰濕": 4, "陽虛": 2},
    "皮膚敏感": {"特稟": 4, "濕熱": 2},
    "症狀:皮膚敏感": {"特稟": 4, "濕熱": 2},
    "容易生氣": {"氣鬱": 4, "濕熱": 2},
    "症狀:容易生氣": {"氣鬱": 4, "濕熱": 2},
    "頭暈": {"血瘀": 2, "氣虛": 2, "痰濕": 1},
    "症狀:頭暈": {"血瘀": 2, "氣虛": 2, "痰濕": 1},
    "夜尿頻繁": {"陽虛": 4, "氣虛": 1},
    "症狀:夜尿頻繁": {"陽虛": 4, "氣虛": 1},
    # ===== 眼睛觀察 =====
    "眼睛:雙眼明亮有神": {},
    "眼睛:眼袋浮腫下垂": {"痰濕": 3, "陽虛": 2},
    "眼睛:黑眼圈深暗": {"血瘀": 3, "陽虛": 2},
    "眼睛:眼瞼淡白": {"氣虛": 3},
    "眼睛:眼神疲倦無神": {"氣虛": 3, "陽虛": 2},
    "眼睛:目赤血絲多": {"濕熱": 4},
    "眼睛:眼睛乾澀充血": {"陰虛": 3},
    # ===== 指甲觀察 =====
    "指甲:月牙適中": {},
    "指甲:月牙過大": {"陽虛": 2, "氣虛": 1},
    "指甲:無月牙": {"陽虛": 3, "氣虛": 2},
    "指甲:甲色蒼白": {"氣虛": 4, "陽虛": 2},
    "指甲:甲色暗紫": {"血瘀": 5},
    "指甲:豎紋明顯": {"氣虛": 3, "陰虛": 2},
    "指甲:橫紋博士線": {"氣虛": 3, "痰濕": 2},
    "指甲:甲面白點": {"氣虛": 3, "痰濕": 2},
    # ===== 舌色觀察 =====
    "舌色:淡紅舌": {},
    "舌色:淡白舌": {"氣虛": 4, "陽虛": 2},
    "舌色:紅舌": {"濕熱": 4, "陰虛": 2},
    "舌色:絳舌": {"濕熱": 4, "陰虛": 3},
    "舌色:紫舌": {"血瘀": 5},
    "舌色:青舌": {"血瘀": 5},
    # ===== 舌形觀察 =====
    "舌形:嫩舌": {"氣虛": 3, "陽虛": 2},
    "舌形:老舌": {"濕熱": 3},
    "舌形:胖大舌": {"痰濕": 5, "陽虛": 3},
    "舌形:瘦薄舌": {"氣虛": 4, "陰虛": 3},
    "舌形:裂紋舌": {"陰虛": 4},
    "舌形:點刺舌": {"濕熱": 4},
    "舌形:齒痕舌": {"氣虛": 5, "痰濕": 3},
    # ===== 舌態觀察 =====
    "舌態:適中": {},
    "舌態:強硬": {"濕熱": 4},
    "舌態:痿軟": {"氣虛": 3, "陰虛": 2},
    "舌態:舌歪斜": {"血瘀": 5},
    # ===== 舌苔色觀察 =====
    "舌苔色:白苔": {"陽虛": 3},
    "舌苔色:黃苔": {"濕熱": 4},
    "舌苔色:灰苔": {"痰濕": 3, "濕熱": 2},
    "舌苔色:黑苔": {"濕熱": 4, "痰濕": 3},
    "舌苔色:無苔": {"陰虛": 4},
    # ===== 舌苔質觀察 =====
    "舌苔質:薄苔": {},
    "舌苔質:厚苔": {"痰濕": 4},
    "舌苔質:潤苔": {},
    "舌苔質:燥苔": {"陰虛": 3, "濕熱": 2},
    "舌苔質:滑苔": {"痰濕": 4, "陽虛": 3},
    "舌苔質:膩苔": {"痰濕": 5},
    "舌苔質:腐苔": {"痰濕": 4, "濕熱": 3},
    "舌苔質:花剝苔": {"氣虛": 4, "陰虛": 3},
    "舌苔質:鏡面苔": {"氣虛": 5, "陰虛": 4},
    # ===== 舌苔形觀察 =====
    "舌苔形:顆粒苔": {"濕熱": 3},
    "舌苔形:膩苔": {"痰濕": 4},
    # ===== 面色觀察 =====
    "面色:面色紅潤": {},
    "面色:面色淡白": {"氣虛": 4},
    "面色:面色萎黃": {"痰濕": 5, "氣虛": 3},
    "面色:面色黧黑": {"陽虛": 4, "血瘀": 3},
    "面色:油光": {"濕熱": 3},
    "面色:浮腫": {"痰濕": 3, "陽虛": 2},
    "面色:潮紅爆痘": {"濕熱": 4},
    "面色:潮紅顴紅": {"陰虛": 4},
    # ===== 體型觀察 =====
    "體型:勻稱適中": {},
    "體型:腹部肥胖": {"痰濕": 5},
    "體型:體型瘦削": {"氣虛": 3},
    "體型:姿態委靡": {"氣虛": 4, "陽虛": 3},
    # ===== 聲音觀察 =====
    "聲音:語速快急": {"氣鬱": 3, "濕熱": 2},
    "聲音:頻繁嘆氣": {"氣鬱": 5},
    "聲音:低微無力": {"氣虛": 5},
    "聲音:痰音粗重": {"痰濕": 5},
    # ===== 唇色觀察 =====
    "唇色:淡紅紅潤": {},
    "唇色:淡白無血": {"氣虛": 4},
    "唇色:萎黃無華": {"氣虛": 4},
    "唇色:青紫暗紫": {"血瘀": 5},
    "唇色:乾裂口角炎": {"濕熱": 4, "陰虛": 2},
}


def analyze_constitution(answers: Dict[str, Any]) -> Dict[str, Any]:
    scores: Dict[str, int] = {k: 0 for k in CONSTITUTION_RULES}
    symptoms = answers.get("symptoms", [])
    for symptom in symptoms:
        # 直接使用原始值（如 "症狀:睡眠差"），映射表用完整key
        weights = OBSERVATION_CONSTITUTION_MAP.get(symptom, {})
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
    phone_idx = next((i for i, h in enumerate(header) if "電話" in h), None)
    if id_idx is None or email_idx is None or name_idx is None:
        return partners
    for line in lines[1:]:
        cells = [cell.strip() for cell in line.split(",")]
        max_idx = max(id_idx, email_idx, name_idx)
        if phone_idx is not None:
            max_idx = max(max_idx, phone_idx)
        if len(cells) <= max_idx:
            continue
        raw_id = cells[id_idx]
        email = cells[email_idx]
        name = cells[name_idx]
        phone = cells[phone_idx] if phone_idx is not None and phone_idx < len(cells) else ""
        if not raw_id or not email:
            continue
        normalized = normalize_partner_id(raw_id)
        if not normalized:
            continue
        entry: Dict[str, str] = {"name": name, "email": email}
        if phone:
            entry["phone"] = phone
        partners[normalized] = entry
    return partners

def refresh_partners_from_sheet() -> Dict[str, Dict[str, str]]:
    global PARTNERS_REFRESHED_AT
    try:
        resp = requests.get(GOOGLE_SHEET_CSV_URL, timeout=15)
        if resp.status_code == 200 and resp.text.strip():
            PARTNERS_CACHE.clear()
            PARTNERS_CACHE.update(parse_sheet_csv(resp.text))
            PARTNERS_REFRESHED_AT = __import__('time').time()
            logger.info("[SHEET] Partners cache refreshed: %d entries", len(PARTNERS_CACHE))
        else:
            logger.warning("[SHEET] Failed to fetch partners CSV: status=%d", resp.status_code)
    except Exception as e:
        logger.warning("[SHEET] Error refreshing partners: %s", e)
    return PARTNERS_CACHE

def get_partners() -> Dict[str, Dict[str, str]]:
    import time as _time
    global PARTNERS_LOADED, PARTNERS_REFRESHED_AT
    now = _time.time()
    if not PARTNERS_LOADED or (now - PARTNERS_REFRESHED_AT > REFRESH_INTERVAL_SECONDS and PARTNERS_REFRESHED_AT > 0):
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

# 預設聯絡電話（查無檢測員時使用，國際格式）
DEFAULT_INSPECTOR_PHONE = os.getenv("DEFAULT_INSPECTOR_PHONE", "85252706980")
DEFAULT_INSPECTOR_NAME = os.getenv("DEFAULT_INSPECTOR_NAME", "客服專員")

def find_inspector_phone(referral_code: Optional[str]) -> str:
    """根據 referral_code 查詢對應檢測員電話，找不到則回傳預設值"""
    partner = find_partner(referral_code)
    if partner and partner.get("phone"):
        return partner["phone"]
    return DEFAULT_INSPECTOR_PHONE

def find_inspector_name(referral_code: Optional[str]) -> str:
    """根據 referral_code 查詢對應檢測員暱稱，找不到則回傳預設值"""
    partner = find_partner(referral_code)
    if partner and partner.get("name"):
        return partner["name"]
    return DEFAULT_INSPECTOR_NAME

def find_inspector_email(referral_code: Optional[str]) -> Optional[str]:
    """根據 referral_code 查詢對應檢測員電郵，找不到則回傳 None（不發送郵件）"""
    partner = find_partner(referral_code)
    if partner and partner.get("email"):
        return partner["email"]
    return None

# ===== HTML 郵件範本生成 =====
# ===== Google Sheets 客戶記錄寫入 =====
def write_customer_record_to_sheet(assessment: Assessment, observations: List[str], contacted: int = 0) -> None:
    """將客戶記錄寫入 Google Sheet「客戶記錄」工作表（11 欄）"""
    import traceback as tb
    try:
        logger.info("[SHEET] Starting write for assessment %s (contacted=%s)", assessment.id, contacted)
        from google.oauth2.service_account import Credentials
        import gspread

        creds = None
        # 優先使用環境變數（雲端部署推薦）
        if GOOGLE_SERVICE_ACCOUNT_JSON:
            try:
                json_data = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
                creds = Credentials.from_service_account_info(json_data, scopes=["https://www.googleapis.com/auth/spreadsheets"])
                logger.info("[SHEET] Using SERVICE_ACCOUNT_JSON credential")
            except Exception as e:
                logger.warning("[SHEET] Failed to parse GOOGLE_SERVICE_ACCOUNT_JSON: %s", e)
        # fallback: 讀取憑證檔案
        if creds is None and os.path.exists(GOOGLE_CREDENTIALS_FILE):
            try:
                creds = Credentials.from_service_account_file(
                    GOOGLE_CREDENTIALS_FILE,
                    scopes=["https://www.googleapis.com/auth/spreadsheets"]
                )
                logger.info("[SHEET] Using credentials file: %s", GOOGLE_CREDENTIALS_FILE)
            except Exception as e:
                logger.warning("[SHEET] Failed to load credentials file: %s", e)

        if creds is None:
            logger.error("[SHEET] No Google credentials available (set GOOGLE_SERVICE_ACCOUNT_JSON or GOOGLE_CREDENTIALS_FILE)")
            return

        gc = gspread.authorize(creds)
        sh = gc.open_by_key(GOOGLE_CUSTOMER_SHEET_ID)
        # 使用 gid=1552781706 對應「客戶記錄」工作表
        CUSTOMER_RECORD_GID = 1552781706
        ws = sh.worksheet(CUSTOMER_RECORD_GID)
        logger.info("[SHEET] Connected to sheet gid=%d", CUSTOMER_RECORD_GID)

        # 確保表頭存在（11 欄）
        headers = ws.row_values(1)
        expected_headers = ["日期及時間", "檢測員", "姓名", "聯絡電話", "性別", "年齡", "體質", "觀察項", "分數", "有否聯絡檢測員", "備註"]
        if not headers or headers[0] != expected_headers[0]:
            ws.update("A1:K1", [expected_headers])
            logger.info("[SHEET] Written headers: %s", expected_headers)

        # 格式化觀察項摘要（完整，不限前10項）
        obs_summary = ", ".join(observations) if observations else "-"

        # 聯絡狀態
        contacted_status = "有" if contacted else "未"
        notes = "AI網頁檢測"

        # 檢測員顯示：有 referral_code 顯示代碼，否則顯示預設
        inspector_display = (assessment.referral_code or "預設").strip()

        # 11 欄資料
        row_data = [
            [assessment.submitted_at.strftime("%Y-%m-%d %H:%M:%S") if assessment.submitted_at else "-"],
            [inspector_display],
            [assessment.name or "-"],
            [assessment.phone or "-"],
            [assessment.gender or "-"],
            [assessment.age_group or "-"],
            [assessment.constitution or "-"],
            [obs_summary],
            [str(assessment.health_score) if assessment.health_score else "-"],
            [contacted_status],
            [notes],
        ]
        logger.info("[SHEET] Row data: %s", row_data)
        ws.values_append(
            f"{CUSTOMER_RECORD_GID}!A:K",
            value_input_option="USER_ENTERED",
            body={"values": [row_data[i][0] for i in range(len(row_data))]}
        )
        logger.info("[SHEET] ✅ Customer record written successfully for assessment %s (contacted=%s)", assessment.id, contacted_status)
    except Exception as e:
        logger.error("[SHEET] ❌ Failed to write customer record to Google Sheet: %s\n%s", e, tb.format_exc())


def update_contact_status_in_sheet(assessment_id: str) -> None:
    """找到最後一行「有否聯絡檢測員」為「未」的列，更新為「有」"""
    import traceback as tb
    try:
        logger.info("[SHEET] Updating contact status for assessment %s", assessment_id)
        from google.oauth2.service_account import Credentials
        import gspread

        creds = None
        if GOOGLE_SERVICE_ACCOUNT_JSON:
            try:
                json_data = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
                creds = Credentials.from_service_account_info(json_data, scopes=["https://www.googleapis.com/auth/spreadsheets"])
            except Exception:
                pass
        if creds is None and os.path.exists(GOOGLE_CREDENTIALS_FILE):
            creds = Credentials.from_service_account_file(
                GOOGLE_CREDENTIALS_FILE,
                scopes=["https://www.googleapis.com/auth/spreadsheets"]
            )
        if creds is None:
            logger.warning("[SHEET] No credentials for contact status update")
            return

        gc = gspread.authorize(creds)
        sh = gc.open_by_key(GOOGLE_CUSTOMER_SHEET_ID)
        # 使用 gid=1552781706 對應「客戶記錄」工作表
        CUSTOMER_RECORD_GID = 1552781706
        ws = sh.worksheet(CUSTOMER_RECORD_GID)
        logger.info("[SHEET] Connected to sheet gid=%d for contact update", CUSTOMER_RECORD_GID)

        all_rows = ws.get_all_values()
        logger.info("[SHEET] Total rows in sheet: %d", len(all_rows))

        if len(all_rows) <= 1:
            logger.warning("[SHEET] No data rows found")
            return

        # 從最後一行往前找「有否聯絡檢測員」=「未」的列
        for row_idx in range(len(all_rows) - 1, 0, -1):
            row = all_rows[row_idx]
            if len(row) >= 10 and row[9] == "未":
                ws.update(f"J{row_idx + 1}", [["有"]])
                logger.info("[SHEET] ✅ Updated contact status to 有 at row %d", row_idx + 1)
                return

        logger.warning("[SHEET] No row with '未' found for contact update")
    except Exception as e:
        logger.error("[SHEET] ❌ Failed to update contact status: %s\n%s", e, tb.format_exc())


def build_email_html(assessment: Assessment, recommendations: List[str]) -> str:
    score = assessment.health_score
    risk = assessment.risk_level
    constitution = assessment.constitution
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
              <h1 style="margin:0; font-size:20px;">COSWAY 新客戶檢測通知</h1>
              <p style="margin:4px 0 0; opacity:0.85; font-size:12px;">提交時間：{submitted_at}</p>
              <p style="margin:4px 0 0; opacity:0.75; font-size:11px;">請盡快透過 WhatsApp 與客戶聯繫跟進</p>
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
    subject = f"【COSWAY 健康檢測通知】新客戶已完成 AI 中醫體質檢測！"
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

# ===== Admin Email Fallback =====
# ADMIN_EMAIL already defined above

# ===== FastAPI 應用 =====
app = FastAPI(title="COSWAY TCM Health Assessment API", version="4.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://healthcheck-bel.pages.dev",
        "https://healthcheck.acesc01gp.workers.dev",
        "http://localhost:8080",
        "http://localhost:3000",
        "*",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 健康檢查路由（避免 Render 404）
@app.get("/")
def root():
    return {"status": "ok"}

@app.get("/api/health")
def health():
    return {"status": "ok"}

@app.get("/health")
def health_legacy():
    return {"status": "ok", "time": datetime.utcnow().isoformat()}

@app.get("/api/referral/validate")
def validate_referral(code: str = None):
    """驗證檢測員號碼是否存在於 Google Sheet 白名單中（含 5 分鐘快取）"""
    if not code:
        return {"valid": False, "message": "missing code"}
    normalized = normalize_partner_id(code)
    if not normalized:
        return {"valid": False, "message": "invalid code format"}
    partner = find_partner(normalized)
    if partner:
        return {
            "valid": True,
            "code": code,
            "normalized": normalized,
            "name": partner.get("name", ""),
            "phone": partner.get("phone", DEFAULT_INSPECTOR_PHONE),
        }
    return {"valid": False, "message": "not found in whitelist"}

@app.post("/api/questionnaire/submit")
def submit_questionnaire(
    data: QuestionnairesSubmit,
    background_tasks: BackgroundTasks
):
    """提交問卷並生成報告，返回完整 JSON 結構"""
    try:
        # 體質分析
        analysis = analyze_constitution(data.answers)
        
        # 分離潛在與嚴重風險
        risk_indicators = analysis.get("risk_indicators", [])
        split_idx = max(1, len(risk_indicators) // 2)
        potential_risks = risk_indicators[:split_idx] if split_idx < len(risk_indicators) else risk_indicators
        severe_risks = risk_indicators[split_idx:] if split_idx < len(risk_indicators) else []
        
        # 儲存至資料庫
        db: Session = SessionLocal()
        try:
            partner = find_partner(data.referral_code)
            record = Assessment(
                name=data.name,
                phone=data.phone,
                gender=data.gender,
                age_group=data.age_group,
                referral_code=data.referral_code,
                answers=json.dumps(data.answers, ensure_ascii=False),
                constitution=analysis["constitution"],
                health_score=analysis["score"],
                risk_level=analysis["risk_level"],
                risk_indicators=json.dumps(risk_indicators, ensure_ascii=False),
                partner_email=partner["email"] if partner else None,
                partner_name=partner["name"] if partner else None,
            )
            db.add(record)
            db.commit()
            db.refresh(record)
            
            # 準備 Email 發送任務（非同步背景任務）
            recommendations = get_recommendations(analysis["constitution"])
            
            # 確定接收電郵：僅發送給對應檢測員，無檢測員則略過
            inspector_email = find_inspector_email(data.referral_code)
            
            if inspector_email and SMTP_ENABLED:
                # 註冊背景任務，非同步發送 Email
                background_tasks.add_task(
                    send_email_report,
                    inspector_email,
                    record,
                    recommendations
                )
                logger.info("Email background task scheduled for %s (to: %s)", record.id, inspector_email)
            elif not SMTP_ENABLED:
                logger.info("SMTP not configured; skip email for assessment %s", record.id)
            else:
                logger.info("No inspector email found for referral_code=%s; skip email", data.referral_code)
            
            # 寫入 Google Sheets 客戶記錄（背景任務，不阻塞回應；initial contacted=0）
            symptoms = data.answers.get("symptoms", [])
            background_tasks.add_task(write_customer_record_to_sheet, record, symptoms, 0)
            logger.info("Google Sheets background task scheduled for %s", record.id)
            
            # 返回完整報告 JSON
            inspector_phone = find_inspector_phone(data.referral_code)
            inspector_name = find_inspector_name(data.referral_code)
            return {
                "success": True,
                "assessment_id": record.id,
                "referral_code": record.referral_code,
                "inspector_phone": inspector_phone,
                "inspector_name": inspector_name,
                "report": {
                    "constitution": analysis["constitution"],
                    "score": analysis["score"],
                    "risk_level": analysis["risk_level"],
                    "potential_risks": potential_risks,
                    "severe_risks": severe_risks,
                    "risk_indicators": risk_indicators,
                },
                "email_sent": inspector_email is not None and SMTP_ENABLED,
                "email_target": inspector_email,
                "email_status": "queued" if inspector_email else "skipped (no inspector)",
            }
        finally:
            db.close()
    except Exception as e:
        logger.exception("Submit questionnaire failed: %s", e)
        return {
            "success": False,
            "error": str(e),
            "report": {
                "constitution": "未知",
                "score": 50,
                "risk_level": "中",
                "potential_risks": ["系統錯誤，請重試"],
                "severe_risks": [],
                "risk_indicators": [],
            },
        }

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
        )
    finally:
        db.close()

@app.post("/api/contact/update")
def update_contact_status(data: BookingSubmit, background_tasks: BackgroundTasks):
    """用戶點擊 WhatsApp 按鈕後，標記為已聯絡檢測員"""
    db: Session = SessionLocal()
    try:
        record = db.query(Assessment).filter(Assessment.id == data.assessment_id).first()
        if not record:
            raise HTTPException(status_code=404, detail="未找到檢測記錄")
        record.contacted = 1
        db.commit()
        # 更新 Sheet 最後一行的「有否聯絡檢測員」為「有」
        background_tasks.add_task(update_contact_status_in_sheet, data.assessment_id)
        return {"success": True, "message": "聯絡狀態已更新"}
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

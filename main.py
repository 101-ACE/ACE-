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
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from io import BytesIO
from typing import Optional, Dict, Any, List
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.image import MIMEImage
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sqlalchemy import create_engine, Column, String, Integer, DateTime, Text, Float
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session

# ===== 時區設定：香港時間 (UTC+8) =====
HK_TZ = ZoneInfo("Asia/Hong_Kong")

def now_hk() -> datetime:
    """回傳当前香港時間（UTC+8）"""
    return datetime.now(HK_TZ)

def to_hk(dt: datetime) -> datetime:
    """將任意 datetime 轉換為香港時間"""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=HK_TZ)
    return dt.astimezone(HK_TZ)


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ===== 配置 =====
DATABASE_URL = "sqlite:///./tcm_assessments.db"

# ===== 靜默 Email 設定（環境變數，具備 Graceful Fallback） =====
SMTP_SERVER = os.getenv("SMTP_HOST", os.getenv("SMTP_SERVER", "smtp.gmail.com"))
# 優先使用 SSL 端口 465；若環境變數指定了其他端口則採用該值
SMTP_PORT = int(os.getenv("SMTP_PORT", "465"))
# 強制指定寄件端為總部信箱
SMTP_USERNAME = "acesc01gp@gmail.com"
# 優先讀 GMAIL_APP_PASSWORD，fallback 到 SMTP_PASSWORD
SMTP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", os.getenv("SMTP_PASSWORD", ""))
SENDER_EMAIL = "acesc01gp@gmail.com"

# 管理員備用電郵
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "acesc01gp@gmail.com")

# 判斷 SMTP 是否可實際使用（密碼未配置時視為未啟用）
SMTP_ENABLED = bool(SMTP_PASSWORD)

# ===== 雲端 Email API 設定（繞過 SMTP 網絡限制）=====
EMAIL_API_KEY = os.getenv("EMAIL_API_KEY", "")
EMAIL_PROVIDER = os.getenv("EMAIL_PROVIDER", "sendgrid").lower()  # "sendgrid" or "resend"
API_EMAIL_ENABLED = bool(EMAIL_API_KEY)

# 使用 API 優先於 SMTP
EMAIL_ENABLED = API_EMAIL_ENABLED or SMTP_ENABLED

# ===== CRITICAL: 憑證缺失偵測 =====
if not SMTP_PASSWORD and not EMAIL_API_KEY:
    print("[CRITICAL] Missing email credentials! Set EMAIL_API_KEY (recommended) or GMAIL_APP_PASSWORD/SMTP_PASSWORD.")
    logger.critical("[CRITICAL] No email credentials configured. Email will NOT be sent.")
elif not EMAIL_API_KEY:
    print("[CONFIG_WARN] EMAIL_API_KEY not set, falling back to SMTP (may be blocked by network)")
    logger.warning("[CONFIG_WARN] EMAIL_API_KEY missing, using SMTP fallback")
elif not SMTP_PASSWORD:
    print("[CONFIG_OK] Using EMAIL_API_KEY for cloud email delivery")
    logger.info("[CONFIG_OK] Cloud email API configured")

# 總部收信信箱（散客/無主客戶專用）
DEFAULT_EMAIL_TARGET = os.getenv("DEFAULT_EMAIL_TARGET", "acesc01gp@gmail.com")

# Google Sheet 設定 - 拍檔名單
GOOGLE_SHEET_ID = "1iZaN0AKtQT6d9zFc_K7tmWjN15q2WpytAN26kxu32H4"
GOOGLE_SHEET_CSV_URL = f"https://docs.google.com/spreadsheets/d/{GOOGLE_SHEET_ID}/export?format=csv"

# Google Sheet 設定 - 客戶記錄 (自動寫入，使用同一份 Sheet 的不同分頁)
GOOGLE_CUSTOMER_SHEET_ID = "1iZaN0AKtQT6d9zFc_K7tmWjN15q2WpytAN26kxu32H4"
GOOGLE_CUSTOMER_SHEET_NAME = "客戶記錄"
GOOGLE_CREDENTIALS_FILE = os.getenv("GOOGLE_CREDENTIALS", "credentials.json")
# 優先順序：GOOGLE_CREDENTIALS_JSON > GOOGLE_SERVICE_ACCOUNT_JSON > 檔案
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON", "")
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")
PARTNERS_CACHE: Dict[str, Dict[str, str]] = {}
PARTNERS_LOADED = False
PARTNERS_REFRESHED_AT: float = 0.0
REFRESH_INTERVAL_SECONDS = 300  # 5 分鐘快取

# ═══════════════════════════════════════════════════════════════
# [FUNNEL] 轉換漏斗追蹤（記憶體統計）
# ═══════════════════════════════════════════════════════════════
class FunnelMetrics:
    """轉換漏斗日誌統計"""
    def __init__(self):
        self.visits: Dict[str, int] = {}        # date -> count
        self.submissions: Dict[str, int] = {}   # date -> count
        self.sheet_writes: Dict[str, int] = {}  # date -> count
        self.email_sends: Dict[str, int] = {}   # date -> count
        self.attribution: Dict[str, List[Dict]] = {}  # date -> [{code, name}]
        self.drops: List[Dict] = []            # 漏斗流失記錄

    def record_visit(self) -> str:
        today = now_hk().strftime("%Y-%m-%d")
        self.visits[today] = self.visits.get(today, 0) + 1
        return today

    def record_submission(self, referral_code: Optional[str], inspector_name: str) -> str:
        today = now_hk().strftime("%Y-%m-%d")
        self.submissions[today] = self.submissions.get(today, 0) + 1
        # 記錄歸因
        if today not in self.attribution:
            self.attribution[today] = []
        self.attribution[today].append({
            "code": referral_code or "預設散客",
            "name": inspector_name,
            "timestamp": now_hk().isoformat()
        })
        return today

    def record_sheet_write(self, success: bool):
        today = now_hk().strftime("%Y-%m-%d")
        if success:
            self.sheet_writes[today] = self.sheet_writes.get(today, 0) + 1
        else:
            # 記錄流失
            self.drops.append({
                "type": "sheet_write_failed",
                "date": today,
                "timestamp": now_hk().isoformat()
            })

    def record_email_send(self, success: bool):
        today = now_hk().strftime("%Y-%m-%d")
        if success:
            self.email_sends[today] = self.email_sends.get(today, 0) + 1
        else:
            self.drops.append({
                "type": "email_send_failed",
                "date": today,
                "timestamp": now_hk().isoformat()
            })

    def get_daily_stats(self, days: int = 7) -> Dict:
        today = now_hk()
        result = {
            "generated_at": now_hk().isoformat(),
            "daily_stats": [],
            "total_visits": sum(self.visits.values()),
            "total_submissions": sum(self.submissions.values()),
            "total_sheet_writes": sum(self.sheet_writes.values()),
            "total_email_sends": sum(self.email_sends.values()),
            "total_drops": len(self.drops),
        }
        for i in range(days):
            date = (today - __import__('datetime').timedelta(days=i)).strftime("%Y-%m-%d")
            result["daily_stats"].append({
                "date": date,
                "visits": self.visits.get(date, 0),
                "submissions": self.submissions.get(date, 0),
                "sheet_writes": self.sheet_writes.get(date, 0),
                "email_sends": self.email_sends.get(date, 0),
                "drop_events": len([d for d in self.drops if d["date"] == date]),
            })
        # 計算整體轉換率
        if result["total_visits"] > 0:
            result["visit_to_submission_rate"] = f"{result['total_submissions']/result['total_visits']*100:.1f}%"
        else:
            result["visit_to_submission_rate"] = "N/A"
        if result["total_submissions"] > 0:
            result["submission_to_sheet_rate"] = f"{result['total_sheet_writes']/result['total_submissions']*100:.1f}%"
            result["submission_to_email_rate"] = f"{result['total_email_sends']/result['total_submissions']*100:.1f}%"
        else:
            result["submission_to_sheet_rate"] = "N/A"
            result["submission_to_email_rate"] = "N/A"
        return result

    def get_attribution_report(self, days: int = 7) -> Dict:
        """生成歸因報告"""
        today = now_hk()
        result = {"generated_at": now_hk().isoformat(), "daily_attribution": {}}
        for i in range(days):
            date = (today - __import__('datetime').timedelta(days=i)).strftime("%Y-%m-%d")
            if date in self.attribution:
                # 統計各檢測員數量
                inspector_counts = {}
                for item in self.attribution[date]:
                    name = item["name"]
                    if name not in inspector_counts:
                        inspector_counts[name] = {"code": item["code"], "count": 0}
                    inspector_counts[name]["count"] += 1
                result["daily_attribution"][date] = list(inspector_counts.values())
        return result

funnel = FunnelMetrics()

# ═══════════════════════════════════════════════════════════════
# [CONFIG_ERROR] 環境變數健康檢查（啟動時執行）
# ═══════════════════════════════════════════════════════════════
_missing_env = []
_required_sheet_vars = ["GOOGLE_CREDENTIALS_JSON", "GOOGLE_SERVICE_ACCOUNT_JSON", "GOOGLE_CREDENTIALS"]
_found_sheet_cred = any(os.getenv(v) for v in _required_sheet_vars)
if not _found_sheet_cred:
    _missing_env.append("GOOGLE_CREDENTIALS_JSON / GOOGLE_SERVICE_ACCOUNT_JSON / GOOGLE_CREDENTIALS")
_required_email_vars = ["EMAIL_API_KEY", "GMAIL_APP_PASSWORD", "SMTP_PASSWORD"]
_found_email_cred = any(os.getenv(v) for v in _required_email_vars)
if not _found_email_cred:
    _missing_env.append("EMAIL_API_KEY / GMAIL_APP_PASSWORD / SMTP_PASSWORD")
if _missing_env:
    for var_group in _missing_env:
        print(f"[CONFIG_ERROR] 缺少必要環境變數: {var_group}")
        logger.error("[CONFIG_ERROR] Missing required env vars: %s", var_group)
else:
    print("[CONFIG_OK] 所有必要環境變數已設定")

# 啟動時印出所有關鍵變數狀態（掩碼密碼）
def _print_config_status():
    import re
    print("\n" + "=" * 60)
    print("  [CONFIG_STATUS] 環境變數狀態檢查")
    print("=" * 60)
    vars_to_check = [
        ("GOOGLE_CREDENTIALS_JSON", GOOGLE_CREDENTIALS_JSON),
        ("GOOGLE_SERVICE_ACCOUNT_JSON", GOOGLE_SERVICE_ACCOUNT_JSON),
        ("GOOGLE_CUSTOMER_SHEET_ID", GOOGLE_CUSTOMER_SHEET_ID),
        ("EMAIL_API_KEY", EMAIL_API_KEY),
        ("EMAIL_PROVIDER", EMAIL_PROVIDER),
        ("GMAIL_APP_PASSWORD", os.getenv("GMAIL_APP_PASSWORD", "")),
        ("SMTP_PASSWORD", os.getenv("SMTP_PASSWORD", "")),
        ("SMTP_SERVER", SMTP_SERVER),
        ("SMTP_PORT", str(SMTP_PORT)),
        ("SENDER_EMAIL", SENDER_EMAIL),
        ("DEFAULT_EMAIL_TARGET", DEFAULT_EMAIL_TARGET),
    ]
    for name, value in vars_to_check:
        if not value:
            print(f"  ❌ {name}: [未設定]")
        elif name in ("GMAIL_APP_PASSWORD", "SMTP_PASSWORD", "GOOGLE_CREDENTIALS_JSON", "GOOGLE_SERVICE_ACCOUNT_JSON", "EMAIL_API_KEY"):
            # 掩碼顯示，只顯示前 4 字元
            masked = f"{value[:4]}...{value[-4:]}" if len(value) > 8 else "***"
            print(f"  ✅ {name}: [{masked}]")
        else:
            print(f"  ✅ {name}: {value}")
    print("=" * 60 + "\n")

_print_config_status()

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
    submitted_at = Column(DateTime, default=now_hk)
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
    risk_indicator_pcts: Optional[List[Dict[str, Any]]] = None  # [{"name":"血管彈性指數","pct":36},...]

# ═══════════════════════════════════════════════════════════════
# [SINGLE SOURCE OF TRUTH] 體質全名 / 建議 / 門市項目
# ═══════════════════════════════════════════════════════════════
CONSTITUTION_FULL_NAMES: Dict[str, str] = {
    "氣虛": "氣虛質",
    "陽虛": "陽虛水泛型",
    "陰虛": "陰虛火旺型",
    "痰濕": "痰濕積聚型",
    "濕熱": "濕熱發炎型",
    "血瘀": "血瘀障礙型",
    "氣鬱": "氣鬱高壓型",
    "特稟": "特稟過敏質",
    "平和": "平和質",
}

def get_constitution_full(constitution: str) -> str:
    return CONSTITUTION_FULL_NAMES.get(constitution, constitution)

RECOMMENDATIONS: Dict[str, List[str]] = {
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

STORE_ITEMS: Dict[str, str] = {
    "氣虛": "黃芪枸杞茶 + 氣血双補精華",
    "陽虛": "紅棗桂圓茶 + 溫陽調理套組",
    "陰虛": "銀耳百合茶 + 滋陰潤燥配方",
    "痰濕": "決明子茶 + 祛濕輕身套組",
    "濕熱": "菊花決明子茶 + 清肝解毒配方",
    "血瘀": "玫瑰花山楂茶 + 活血化瘀套組",
    "氣鬱": "玫瑰茉莉茶 + 疏肝解鬱配方",
    "特稟": "蘆薈清潤茶 + 抗敏調理套組",
    "平和": "日常保健茶 + 均衡營養配方",
}

# 健康風險關注指標：三項固定名稱 + 計算公式（與圖片/網頁完全一致）
RISK_INDICATOR_CONFIG: List[tuple] = [
    ("血管彈性指數", 0.9, 5.0),
    ("血液粘稠度",  0.8, 0.0),
    ("肝臟代謝負擔",1.0, 0.0),
]

def calc_risk_indicator_pct(base_score: int, factor: float, offset: float) -> int:
    """與 generate_report_image / build_email_html 完全一致的風險指標百分比"""
    return min(100, max(5, round((100 - base_score) * factor + offset)))

def get_risk_indicator_pcts(score: int) -> List[Dict[str, Any]]:
    return [{"name": name, "pct": calc_risk_indicator_pct(score, f, o)}
            for name, f, o in RISK_INDICATOR_CONFIG]

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

def _strip_852(phone: str) -> str:
    """去除 852/+852 國際區號，只保留本地 8 位數字"""
    if not phone:
        return phone
    p = str(phone).strip()
    if p.startswith('+852'):
        return p[4:]
    if p.startswith('852'):
        return p[3:]
    return p

def find_inspector_phone(referral_code: Optional[str]) -> str:
    """根據 referral_code 查詢對應檢測員電話，找不到則回傳預設值（已去除 852 區號）"""
    partner = find_partner(referral_code)
    if partner and partner.get("phone"):
        return _strip_852(partner["phone"])
    return _strip_852(DEFAULT_INSPECTOR_PHONE)

def find_inspector_name(referral_code: Optional[str]) -> str:
    """根據 referral_code 查詢對應檢測員暱稱，找不到則回傳預設值"""
    partner = find_partner(referral_code)
    if partner and partner.get("name"):
        return partner["name"]
    return DEFAULT_INSPECTOR_NAME

def find_inspector_email(referral_code: Optional[str]) -> str:
    """根據 referral_code 查詢對應檢測員電郵；查無則回傳總部信箱（绝不回傳 None）"""
    partner = find_partner(referral_code)
    if partner and partner.get("email"):
        return partner["email"]
    # 兜底：發送至總部
    return DEFAULT_EMAIL_TARGET

# ===== HTML 郵件範本生成 =====
# ===== Google Sheets 客戶記錄寫入 =====
def write_customer_record_to_sheet(assessment: Assessment, observations: List[str], contacted: int = 0) -> None:
    """將客戶記錄寫入 Google Sheet「客戶記錄」工作表（11 欄）"""
    try:
        logger.info("[SHEET] Starting write for assessment %s (contacted=%s)", assessment.id, contacted)
        from google.oauth2.service_account import Credentials
        import gspread

        creds = None
        # 【優先順序 1】GOOGLE_CREDENTIALS_JSON（Render 環境變數，最高優先）
        if GOOGLE_CREDENTIALS_JSON:
            try:
                json_data = json.loads(GOOGLE_CREDENTIALS_JSON)
                creds = Credentials.from_service_account_info(json_data, scopes=["https://www.googleapis.com/auth/spreadsheets"])
                print(f"[SHEET_LOG] ✅ 使用 GOOGLE_CREDENTIALS_JSON 憑證")
                logger.info("[SHEET] Using GOOGLE_CREDENTIALS_JSON credential")
            except Exception as e:
                print(f"[SHEET_CRITICAL] ❌ 解析 GOOGLE_CREDENTIALS_JSON 失敗: {e}")
                logger.error("[SHEET] Failed to parse GOOGLE_CREDENTIALS_JSON: %s", e)
        # 【優先順序 2】GOOGLE_SERVICE_ACCOUNT_JSON
        if creds is None and GOOGLE_SERVICE_ACCOUNT_JSON:
            try:
                json_data = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
                creds = Credentials.from_service_account_info(json_data, scopes=["https://www.googleapis.com/auth/spreadsheets"])
                print(f"[SHEET_LOG] ✅ 使用 GOOGLE_SERVICE_ACCOUNT_JSON 憑證")
                logger.info("[SHEET] Using SERVICE_ACCOUNT_JSON credential")
            except Exception as e:
                print(f"[SHEET_CRITICAL] ❌ 解析 GOOGLE_SERVICE_ACCOUNT_JSON 失敗: {e}")
                logger.error("[SHEET] Failed to parse GOOGLE_SERVICE_ACCOUNT_JSON: %s", e)
        # 【優先順序 3】憑證檔案
        if creds is None and os.path.exists(GOOGLE_CREDENTIALS_FILE):
            try:
                creds = Credentials.from_service_account_file(
                    GOOGLE_CREDENTIALS_FILE,
                    scopes=["https://www.googleapis.com/auth/spreadsheets"]
                )
                print(f"[SHEET_LOG] ✅ 使用憑證檔案: {GOOGLE_CREDENTIALS_FILE}")
                logger.info("[SHEET] Using credentials file: %s", GOOGLE_CREDENTIALS_FILE)
            except Exception as e:
                print(f"[SHEET_CRITICAL] ❌ 讀取憑證檔案失敗: {e}")
                logger.error("[SHEET] Failed to load credentials file: %s", e)

        if creds is None:
            print(f"[SHEET_CRITICAL] ❌ 無法取得 Google 憑證！已檢查以下來源：")
            print(f"[SHEET_CRITICAL]    1. GOOGLE_CREDENTIALS_JSON: {'[已設定]' if GOOGLE_CREDENTIALS_JSON else '[未設定]'}")
            print(f"[SHEET_CRITICAL]    2. GOOGLE_SERVICE_ACCOUNT_JSON: {'[已設定]' if GOOGLE_SERVICE_ACCOUNT_JSON else '[未設定]'}")
            print(f"[SHEET_CRITICAL]    3. 憑證檔案 ({GOOGLE_CREDENTIALS_FILE}): {'[存在]' if os.path.exists(GOOGLE_CREDENTIALS_FILE) else '[不存在]'}")
            logger.critical("[SHEET] No Google credentials available. Check env vars: GOOGLE_CREDENTIALS_JSON, GOOGLE_SERVICE_ACCOUNT_JSON")
            return

        gc = gspread.authorize(creds)
        sh = gc.open_by_key(GOOGLE_CUSTOMER_SHEET_ID)
        # 優先透過工作表名稱精確定位（避免 gid 索引型態問題）
        ws = sh.worksheet("客戶記錄")
        logger.info("[SHEET] Connected to worksheet '客戶記錄'")

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

        # 11 欄資料（一維列表，供 append_row 使用）
        row_data = [
            assessment.submitted_at.strftime("%Y-%m-%d %H:%M:%S") if assessment.submitted_at else "-",
            inspector_display,
            assessment.name or "-",
            assessment.phone or "-",
            assessment.gender or "-",
            assessment.age_group or "-",
            assessment.constitution or "-",
            obs_summary,
            str(assessment.health_score) if assessment.health_score else "-",
            contacted_status,
            notes,
        ]
        logger.info("[SHEET] Row data: %s", row_data)
        ws.append_row(row_data)
        logger.info("[SHEET] ✅ Customer record written successfully for assessment %s (contacted=%s)", assessment.id, contacted_status)
        print(f"[SHEET_LOG] ✅ 寫入客戶記錄成功 | assessment_id={assessment.id} | 體質={assessment.constitution} | 檢測員={inspector_display}")
    except Exception as e:
        _sheet_tb = __import__('traceback').format_exc()
        logger.error("[SHEET] ❌ Failed to write customer record: %s\n%s", e, _sheet_tb)
        print(f"[SHEET_ERROR] ❌ 寫入客戶記錄失敗 | assessment_id={assessment.id}")
        print(f"[SHEET_ERROR]    錯誤: {e}")
        print(f"[SHEET_ERROR]    完整 traceback:\n{_sheet_tb}")


def update_contact_status_in_sheet(assessment_id: str) -> None:
    """找到最後一行「有否聯絡檢測員」為「未」的列，更新為「有」"""
    try:
        logger.info("[SHEET] Updating contact status for assessment %s", assessment_id)
        from google.oauth2.service_account import Credentials
        import gspread

        creds = None
        # 【優先順序 1】GOOGLE_CREDENTIALS_JSON
        if GOOGLE_CREDENTIALS_JSON:
            try:
                json_data = json.loads(GOOGLE_CREDENTIALS_JSON)
                creds = Credentials.from_service_account_info(json_data, scopes=["https://www.googleapis.com/auth/spreadsheets"])
                print(f"[SHEET_LOG] ✅ 使用 GOOGLE_CREDENTIALS_JSON 憑證（聯絡更新）")
            except Exception:
                pass
        # 【優先順序 2】GOOGLE_SERVICE_ACCOUNT_JSON
        if creds is None and GOOGLE_SERVICE_ACCOUNT_JSON:
            try:
                json_data = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
                creds = Credentials.from_service_account_info(json_data, scopes=["https://www.googleapis.com/auth/spreadsheets"])
            except Exception:
                pass
        # 【優先順序 3】憑證檔案
        if creds is None and os.path.exists(GOOGLE_CREDENTIALS_FILE):
            creds = Credentials.from_service_account_file(
                GOOGLE_CREDENTIALS_FILE,
                scopes=["https://www.googleapis.com/auth/spreadsheets"]
            )
        if creds is None:
            print(f"[SHEET_CRITICAL] ❌ 無法取得 Google 憑證（聯絡更新）| assessment_id={assessment_id}")
            logger.warning("[SHEET] No credentials for contact status update")
            return

        gc = gspread.authorize(creds)
        sh = gc.open_by_key(GOOGLE_CUSTOMER_SHEET_ID)
        # 優先透過工作表名稱精確定位
        ws = sh.worksheet("客戶記錄")
        logger.info("[SHEET] Connected to worksheet '客戶記錄' for contact update")

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


def generate_report_image(assessment: Assessment, recommendations: List[str]) -> Optional[bytes]:
    """生成檢測報告圖片 (PNG) – 2x 高解析度、無 Emoji 亂碼、客戶資料卡拉長"""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        logger.warning("[EMAIL] PIL not available, skipping image generation")
        print("[EMAIL_LOG] ⚠️ PIL 未安裝，跳過圖片生成")
        return None

    try:
        # 字型載入：優先使用專案內置字型，fallback 到系統字型
        _FONT_DIR = os.path.join(os.path.dirname(__file__), "fonts")
        _SYSTEM_FONTS = [
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/noto/NotoSansTC-Regular.ttf",
            "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ]

        def get_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
            project_fonts = []
            if bold:
                project_fonts = [
                    f"NotoSansCJK-Black.ttc",
                    f"NotoSansCJK-Regular.ttc",
                    f"NotoSansCJK-Bold.ttf",
                    f"NotoSansTC-Bold.ttf",
                    f"NotoSansTC-Regular.ttf"
                ]
            else:
                project_fonts = [
                    f"NotoSansCJK-Regular.ttc",
                    f"NotoSansCJK-Bold.ttc",
                    f"NotoSansCJK-Regular.ttf",
                    f"NotoSansTC-Regular.ttf",
                    f"NotoSansTC-Bold.ttf"
                ]

            for fname in project_fonts:
                fpath = os.path.join(_FONT_DIR, fname)
                if os.path.exists(fpath):
                    return ImageFont.truetype(fpath, size)
            for fpath in _SYSTEM_FONTS:
                if os.path.exists(fpath):
                    return ImageFont.truetype(fpath, size)
            logger.warning("[EMAIL] No Chinese font found, using default font")
            return ImageFont.load_default()

        # ── 2x 高解析度畫布 (600×820 → 1200×1640) ──────────────────
        W, H = 1200, 1640
        img = Image.new('RGB', (W, H), color='#FFF8F0')
        draw = ImageDraw.Draw(img)

        # 所有字級同步 ×2；環下標籤另加放大字級
        font_title    = get_font(52, bold=True)
        font_label    = get_font(28)
        font_value    = get_font(32, bold=True)
        font_small    = get_font(24)
        font_big      = get_font(96, bold=True)
        font_ring_lbl = get_font(45, bold=True)  # 潛在風險等級（45px bold，放大50%）
        font_ring_lbl_b = get_font(38, bold=True)  # 體質判定（38px，放大 25%）
        font_cta      = get_font(36, bold=True)
        font_rec      = get_font(26)
        font_comparison = get_font(46)  # 同齡對比主文案（46px，放大30%）
        font_comparison_num = get_font(65, bold=True)  # 分數/百分比數字（65px bold，放大30%）
        font_bar      = get_font(26)

        # 配色
        ORANGE_DARK  = '#BF360C'
        ORANGE_MAIN  = '#E65100'
        ORANGE_LIGHT = '#FF8F00'
        ORANGE_BG    = '#FFF3E0'
        WHITE        = '#FFFFFF'
        DARK         = '#3E2723'
        GRAY         = '#6D4C41'
        GRAY_LIGHT   = '#E5E7EB'

        score = assessment.health_score
        risk  = assessment.risk_level
        if risk == "高":
            risk_text_color = (0xD3, 0x2F, 0x2F)
        elif risk == "中":
            risk_text_color = (0xF5, 0x7C, 0x00)
        else:
            risk_text_color = (0x38, 0x8E, 0x3C)

        # Score color for ring (4-tier, aligned with spec)
        if score <= 40:
            ring_color = (0xDC, 0x26, 0x26)      # 🔴 紅：0-40 極高風險
        elif score <= 60:
            ring_color = (0x78, 0x35, 0x0F)      # 🟤 褐：41-60 中高風險
        elif score <= 80:
            ring_color = (0xD9, 0x77, 0x06)      # 🟡 黃：61-80 關注
        else:
            ring_color = (0x16, 0xA3, 0x4A)      # 🟢 綠：81-100 良好

        inspector_name = find_inspector_name(assessment.referral_code)
        inspector_phone = find_inspector_phone(assessment.referral_code)

        # ===== 頂部橙色 Hero 區塊（收緊高度，剛包覆提交時間）=====
        draw.rectangle([(0, 0), (W, 170)], fill=ORANGE_MAIN)
        draw.text((W // 2, 38), "快速中醫體質檢測", font=font_title, fill=WHITE, anchor='mm')
        draw.text((W // 2, 96),
                  f"提交時間：{assessment.submitted_at.strftime('%Y-%m-%d %H:%M') if assessment.submitted_at else '-'}",
                  font=font_small, fill='#FFE0B2', anchor='mm')

        y = 186

        # ===== 客戶資料卡片（行距收緊，三行緊湊排列）=====
        draw.rectangle([(40, y), (W - 40, y + 190)], fill=WHITE, outline='#E0E0E0', width=2)
        draw.text((60, y + 16), "客戶資料", font=font_label, fill=ORANGE_MAIN)
        draw.text((60, y + 56), f"姓名：{assessment.name}", font=font_value, fill=DARK)
        draw.text((60, y + 96), f"電話：{assessment.phone}", font=font_value, fill=DARK)
        draw.text((60, y + 140),
                  f"檢測人員：{inspector_name}（{inspector_phone}）",
                  font=get_font(32, bold=True), fill=DARK)
        y += 218

        # ===== 健康警示分數 – Progress Ring 區塊 =====
        ring_center_x = W // 2
        ring_center_y = y + 140
        ring_radius = 81          # 原 40.5 ×2
        ring_width = 14           # 原 7 ×2

        bbox = [(ring_center_x - ring_radius, ring_center_y - ring_radius),
                (ring_center_x + ring_radius, ring_center_y + ring_radius)]
        draw.ellipse(bbox, outline=GRAY_LIGHT, width=ring_width)

        arc_start = -90
        arc_end = arc_start - (score / 100.0) * 360
        draw.arc(bbox, start=arc_start, end=arc_end, fill=ring_color, width=ring_width)

        if score <= 40:
            score_text_color = (0xDC, 0x26, 0x26)     # 🔴 紅：0-40
        elif score <= 60:
            score_text_color = (0x78, 0x35, 0x0F)     # 🟤 褐：41-60
        elif score <= 80:
            score_text_color = (0xD9, 0x77, 0x06)     # 🟡 黃：61-80
        else:
            score_text_color = (0x16, 0xA3, 0x4A)     # 🟢 綠：81-100
        draw.text((ring_center_x, ring_center_y), str(score), font=font_big, fill=score_text_color, anchor='mm')

        draw.text((W // 2, y + 220), f"潛在風險等級：{risk}", font=font_ring_lbl, fill=risk_text_color, anchor='mm')

        # 同齡人群比較文案（連貫單行：三段拼接，消除空白，分數數字加粗）
        _percent = min(99, max(1, int((100 - score) * 0.95)))
        if score >= 80:
            _seg1 = f"您的健康分數為 "
            _seg2 = str(score)
            _seg3 = f" 分，超越約 {(100 - _percent)}% 的同齡人群"
        else:
            _seg1 = f"您的健康分數為 "
            _seg2 = str(score)
            _seg3 = f" 分，低於約 {_percent}% 的同齡人群"
        # 全用 font_comparison 測量（確保字距一致），分三段繪製
        w1 = font_comparison.getlength(_seg1)
        w2 = font_comparison_num.getlength(_seg2)
        w3 = font_comparison.getlength(_seg3)
        total_w = w1 + w2 + w3
        x_start = W // 2 - total_w // 2
        draw.text((x_start,               y + 282), _seg1, font=font_comparison, fill=(0x4B, 0x55, 0x63), anchor='lm')
        draw.text((x_start + w1,          y + 282), _seg2, font=font_comparison_num, fill=score_text_color, anchor='lm')
        draw.text((x_start + w1 + w2,     y + 282), _seg3, font=font_comparison, fill=(0x4B, 0x55, 0x63), anchor='lm')

        # 體質全名（共用模組級常數，禁止簡寫）
        _const_full = get_constitution_full(assessment.constitution)
        draw.text((W // 2, y + 358), f"體質判定：{_const_full}",
                  font=font_ring_lbl_b, fill=GRAY, anchor='mm')
        y += 420

        # ===== 健康風險指標 (Progress Bars) — 共用 calc_risk_indicator_pct =====
        indicators = get_risk_indicator_pcts(score)

        draw.rectangle([(40, y), (W - 40, y + 240)], fill=WHITE, outline='#E0E0E0', width=2)
        draw.text((60, y + 20), "健康風險關注指標", font=font_label, fill=ORANGE_MAIN)

        for i, ind_obj in enumerate(indicators):
            ind = ind_obj["name"]
            pct = ind_obj["pct"]
            bar_y = y + 76 + i * 56
            label_x = 60
            label_y = bar_y + 12

            draw.text((label_x, label_y), ind, font=font_bar, fill=DARK, anchor='lm')

            bar_x = label_x + 220
            bar_width = W - 40 - bar_x - 40
            bar_height = 28
            r = bar_height // 2  # 圓角半徑 = 14
            bar_bg = GRAY_LIGHT
            if pct <= 25:
                bar_fg = (0x10, 0xB9, 0x81)
            elif pct <= 50:
                bar_fg = (0xFB, 0xBF, 0x24)
            elif pct <= 75:
                bar_fg = (0xEF, 0x44, 0x44)
            else:
                bar_fg = (0x78, 0x35, 0x0F)

            # Background pill shape
            draw.rectangle([(bar_x + r, label_y - r), (bar_x + bar_width - r, label_y + r)],
                           fill=bar_bg)
            draw.ellipse([(bar_x, label_y - r), (bar_x + 2 * r, label_y + r)], fill=bar_bg)
            draw.ellipse([(bar_x + bar_width - 2 * r, label_y - r), (bar_x + bar_width, label_y + r)], fill=bar_bg)
            # Foreground pill shape
            fg_width = max(2 * r, int(bar_width * pct / 100))
            draw.rectangle([(bar_x + r, label_y - r), (bar_x + fg_width - r, label_y + r)],
                           fill=bar_fg)
            draw.ellipse([(bar_x, label_y - r), (bar_x + 2 * r, label_y + r)], fill=bar_fg)
            draw.ellipse([(bar_x + fg_width - 2 * r, label_y - r), (bar_x + fg_width, label_y + r)], fill=bar_fg)
            # Percentage label on the right side of the bar
            pct_text = f"{pct}%"
            pct_font = get_font(22)
            pct_w = pct_font.getlength(pct_text)
            draw.text((bar_x + fg_width + 10 + pct_w / 2, label_y), pct_text, font=pct_font, fill=DARK, anchor='lm')

        y += 270

        # 飲食與生活建議（共用 RECOMMENDATIONS 常量）
        rec_title_y = y + 16
        recs = RECOMMENDATIONS.get(assessment.constitution, [])
        card_h = 120 + len(recs) * 40
        draw.rectangle([(40, y), (W - 40, y + card_h)], fill=WHITE, outline='#E0E0E0', width=2)
        draw.text((60, rec_title_y), "飲食與生活建議", font=font_label, fill=ORANGE_MAIN)
        for i, rec in enumerate(recs):
            draw.text((60, rec_title_y + 52 + i * 40), f"• {rec}", font=font_rec, fill=DARK)
        y += card_h + 32

        buf = BytesIO()
        img.save(buf, format='PNG', quality=95)
        buf.seek(0)
        return buf.getvalue()
    except Exception as e:
        logger.warning("[EMAIL] Image generation failed: %s", e)
        print(f"[EMAIL_LOG] ⚠️ 圖片生成失敗: {e}")
        return None


def build_email_html(assessment: Assessment, recommendations: List[str]) -> str:
    score = assessment.health_score
    risk = assessment.risk_level
    constitution = assessment.constitution
    name = assessment.name
    phone = assessment.phone
    inspector_name = find_inspector_name(assessment.referral_code)
    inspector_phone = find_inspector_phone(assessment.referral_code)
    submitted_at = assessment.submitted_at.strftime("%Y-%m-%d %H:%M") if assessment.submitted_at else "-"
    indicators = assessment.risk_indicators or "[]"
    try:
        indicators_list = json.loads(indicators)
    except Exception:
        indicators_list = []
    # 風險指標百分比（共用 calc_risk_indicator_pct，與圖片完全一致）
    bar_data = get_risk_indicator_pcts(score)
    # 使用 RISK_INDICATOR_CONFIG 的標準名稱確保三端一致
    FIXED_INDICATORS = [(cfg[0], item["pct"]) for cfg, item in zip(RISK_INDICATOR_CONFIG, bar_data)]

    # 4-tier score color (red→brown→yellow→green, aligned with PIL image)
    if score <= 40:
        ring_color = "#DC2626"
        risk_circle_color = "#DC2626"
        risk_bg_color = "rgba(220, 38, 38, 0.1)"
    elif score <= 60:
        ring_color = "#78350F"
        risk_circle_color = "#78350F"
        risk_bg_color = "rgba(120, 53, 15, 0.1)"
    elif score <= 80:
        ring_color = "#D97706"
        risk_circle_color = "#D97706"
        risk_bg_color = "rgba(217, 119, 6, 0.1)"
    else:
        ring_color = "#16A34A"
        risk_circle_color = "#16A34A"
        risk_bg_color = "rgba(22, 163, 74, 0.1)"

    # 四色嚴重度進度條：綠(正常) → 黃(輕度) → 紅(中高) → 褐(極高)
    def _bar_color(pct: int) -> str:
        if pct <= 25:
            return "#10B981"    # 🟢 綠：正常/低風險
        elif pct <= 50:
            return "#FBBF24"    # 🟡 黃：輕度風險
        elif pct <= 75:
            return "#EF4444"    # 🟠 紅：中度/高風險
        else:
            return "#78350F"    # 🟤 深褐：極高風險/嚴重預警

    # 生成進度條 HTML
    bars_html = ""
    for item in bar_data:
        ind_name = item["name"]
        pct = item["pct"]
        bc = _bar_color(pct)
        bars_html += (
            '<table width="100%" cellpadding="0" cellspacing="0" style="margin:10px 0;">'
            '<tr>'
            '<td width="130" style="font-size:14px; color:#3E2723; font-weight:600; vertical-align:middle;">'
            + ind_name + '</td>'
            '<td style="vertical-align:middle;">'
            '<table width="100%" cellpadding="0" cellspacing="0" style="background:#E5E7EB; border-radius:7px; overflow:hidden;">'
            '<tr><td width="' + str(pct) + '%" height="14" style="background:' + bc + '; font-size:0; line-height:0;">&nbsp;</td>'
            '<td width="' + str(100 - pct) + '%">&#xa0;</td>'
            '</tr></table></td>'
            '<td width="40" align="right" style="font-size:13px; color:#3E2723;">' + str(pct) + '%</td>'
            '</tr></table>'
        )

    return f"""
    <html>
    <head>
      <meta charset="UTF-8">
      <meta name="viewport" content="width=device-width, initial-scale=1.0">
      <style type="text/css">
        @media screen and (max-width: 600px) {{
          .email-container {{ width: 100% !important; max-width: 100% !important; }}
          .card-block {{ width: 100% !important; box-sizing: border-box !important; }}
          .card-padding {{ padding: 12px 14px !important; }}
          .hero-padding {{ padding: 20px 14px !important; }}
          .score-ring-wrap {{ width: 90px !important; height: 90px !important; }}
          .score-ring-wrap svg {{ width: 90px !important; height: 90px !important; }}
          .score-number {{ font-size: 20px !important; }}
          .ind-label {{ width: 100px !important; font-size: 13px !important; }}
        }}
      </style>
    </head>
    <body style="font-family: Arial, 'Noto Sans TC', sans-serif; background:#FFF8F0; margin:0; padding:0;">
      <table width="100%" cellpadding="0" cellspacing="0" style="background:#FFF8F0; padding:20px 0;">
        <tr><td align="center">
          <table class="email-container" width="600" cellpadding="0" cellspacing="0"
                 style="background:#fff; border-radius:14px; overflow:hidden;
                        box-shadow:0 2px 12px rgba(230,81,0,0.12);
                        width:600px; max-width:600px; margin:0 auto;">

            <!-- ===== 橙色 Hero 區塊 ===== -->
            <tr><td class="hero-padding" style="background:linear-gradient(135deg,#E65100,#FF8F00); color:#fff; padding:28px 24px; text-align:center;">
              <h1 style="margin:0; font-size:22px; font-weight:700;">快速中醫體質檢測</h1>
              <p style="margin:6px 0 0; opacity:0.85; font-size:13px;">提交時間：{submitted_at}</p>
            </td></tr>

            <tr><td style="padding:16px 20px 0;">
              <!-- 客戶基本資料 -->
              <table class="card-block" width="100%" cellpadding="0" cellspacing="0"
                     style="background:#FFF3E0; border:1px solid #FFCC80; border-radius:12px; margin-bottom:14px; width:100% !important; box-sizing:border-box !important;">
                <tr><td class="card-padding" style="padding:14px;">
                  <h3 style="margin:0 0 10px; color:#E65100; font-size:15px;">👤 客戶資料</h3>
                  <div style="font-size:14px; color:#3E2723; line-height:1.8;">
                    <div>姓名：<strong>{name}</strong></div>
                    <div>電話：<strong>{phone}</strong></div>
                    <div>檢測人員：<strong>{inspector_name}（{inspector_phone}）</strong></div>
                  </div>
                </td></tr>
              </table>

              <!-- 健康警示分數 - Progress Ring 區塊 -->
              <table class="card-block" width="100%" cellpadding="0" cellspacing="0"
                     style="background:#FFF8E1; border:1px solid #FFCC80; border-radius:12px; margin-bottom:14px; width:100% !important; box-sizing:border-box !important;">
                <tr><td style="padding:20px; text-align:center;">
                  <div style="font-size:13px; color:#6D4C41;">健康警示分數</div>
                  <div style="margin:12px auto;">
                    <table align="center" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
                      <tr><td align="center" valign="middle"
                            style="position:relative; width:100px; height:100px;">
                        <!-- SVG ring centered via table cell -->
                        <svg width="100" height="100" viewBox="0 0 100 100" style="display:block; margin:0 auto;">
                          <circle cx="50" cy="50" r="45" stroke="#E5E7EB" stroke-width="7" fill="none"/>
                          <circle cx="50" cy="50" r="45" stroke="{ring_color}" stroke-width="7" fill="none"
                                  stroke-dasharray="283" stroke-dashoffset="{283 - int(score / 100 * 283)}"
                                  stroke-linecap="round" transform="rotate(-90 50 50)"/>
                        </svg>
                        <!-- Score number centered with table cell (Gmail/iOS safe) -->
                        <table align="center" cellpadding="0" cellspacing="0" style="margin-top:-100px;">
                          <tr><td align="center" valign="middle"
                                style="font-size:24px; font-weight:700; color:{ring_color}; padding-top:38px;">
                            {score}
                          </td></tr>
                        </table>
                      </td></tr>
                    </table>
                  </div>
                  <div style="margin-top:6px; display:inline-block; padding:6px 16px; border-radius:999px; background:{risk_bg_color}; color:{risk_circle_color}; font-weight:700; font-size:14px;">潛在風險等級：{risk}</div>
                  <div style="margin-top:8px; font-size:14px; color:#3E2723;">體質判定：<strong>{get_constitution_full(constitution)}</strong></div>
                </td></tr>
              </table>

              <!-- 健康風險指標 - Progress Bars 區塊 -->
              <table class="card-block" width="100%" cellpadding="0" cellspacing="0"
                     style="background:#fff; border:1px solid #FFE0B2; border-radius:12px; margin-bottom:14px; width:100% !important; box-sizing:border-box !important;">
                <tr><td class="card-padding" style="padding:14px;">
                  <h3 style="margin:0 0 12px; color:#E65100; font-size:15px;">⚠️ 健康風險關注指標</h3>
                  {bars_html}
                </td></tr>
              </table>

              <!-- 飲食與生活作息建議 -->
              <table class="card-block" width="100%" cellpadding="0" cellspacing="0"
                     style="background:#fff; border:1px solid #FFE0B2; border-radius:12px; margin-bottom:14px; width:100% !important; box-sizing:border-box !important;">
                <tr><td class="card-padding" style="padding:14px;">
                  <h3 style="margin:0 0 10px; color:#E65100; font-size:15px;">🍵 飲食與生活作息建議</h3>
                  {"".join([f"<div style='padding:6px 0; border-bottom:1px solid #FFE0B2; font-size:14px; color:#3E2723;'>• {r}</div>" for r in recommendations])}
                </td></tr>
              </table>

              <!-- COSWAY 推薦門市檢測項目 -->
              <table class="card-block" width="100%" cellpadding="0" cellspacing="0"
                     style="background:#fff; border:1px solid #FFE0B2; border-radius:12px; margin-bottom:14px; width:100% !important; box-sizing:border-box !important;">
                <tr><td class="card-padding" style="padding:14px;">
                  <h3 style="margin:0 0 10px; color:#E65100; font-size:15px;">🏥 COSWAY 推薦門市檢測項目</h3>
                  <div style="font-size:14px; color:#3E2723; line-height:1.8;">
                    根據您的 <strong>{get_constitution_full(constitution)}</strong> 體質，建議前往門市進行：<br/>
                    <strong style="color:#E65100;">{STORE_ITEMS.get(constitution, STORE_ITEMS.get('平和', '微電流磁共檢測'))}</strong>
                  </div>
                </td></tr>
              </table>
            </td></tr>

          </table>
        </td></tr>
      </table>
    </body>
    </html>
    """

# ===== Email 推播服務（詳細日誌 + 完整 traceback 輸出） =====
def _send_via_sendgrid_api(html_content: str, to_email: str, subject: str, from_email: str, image_bytes: bytes = None) -> Dict[str, Any]:
    """使用 SendGrid API (HTTP POST) 發送郵件（支援圖片附件）"""
    import json as _json
    try:
        payload = {
            "personalizations": [{"to": [{"email": to_email}]}],
            "from": {"email": from_email, "name": "COSWAY 健康檢測"},
            "subject": subject,
            "content": [{"type": "text/html", "value": html_content}]
        }
        # 只有當圖片確實存在且不為空時才加入附件
        if image_bytes and len(image_bytes) > 0:
            payload["attachments"] = [{
                "content": base64.b64encode(image_bytes).decode(),
                "filename": "報告圖表.png",
                "type": "image/png",
                "disposition": "attachment"
            }]
        headers = {
            "Authorization": f"Bearer {EMAIL_API_KEY}",
            "Content-Type": "application/json"
        }
        resp = requests.post(
            "https://api.sendgrid.com/v3/mail/send",
            headers=headers,
            json=payload,
            timeout=15
        )
        if resp.status_code in (200, 202):
            return {"success": True, "method": "sendgrid_api", "status": resp.status_code, "has_image": bool(image_bytes)}
        else:
            error_msg = resp.text[:200]
            logger.error("[SENDGRID_ERROR] API response %s: %s", resp.status_code, error_msg)
            return {"success": False, "method": "sendgrid_api", "error": f"HTTP {resp.status_code}: {error_msg}"}
    except requests.exceptions.Timeout:
        return {"success": False, "method": "sendgrid_api", "error": "API request timeout (15s)"}
    except requests.exceptions.ConnectionError as e:
        return {"success": False, "method": "sendgrid_api", "error": f"Connection error: {e}"}
    except Exception as e:
        return {"success": False, "method": "sendgrid_api", "error": str(e)}


def _send_via_resend_api(html_content: str, to_email: str, subject: str, from_email: str, image_bytes: bytes = None) -> Dict[str, Any]:
    """使用 Resend API (HTTP POST) 發送郵件（支援圖片附件）"""
    import json as _json
    try:
        payload = {
            "from": f"COSWAY 健康檢測 <{from_email}>",
            "to": [to_email],
            "subject": subject,
            "html": html_content
        }
        # 只有當圖片確實存在且不為空時才加入附件
        if image_bytes and len(image_bytes) > 0:
            payload["attachments"] = [{
                "filename": "報告圖表.png",
                "content": base64.b64encode(image_bytes).decode()
            }]
        headers = {
            "Authorization": f"Bearer {EMAIL_API_KEY}",
            "Content-Type": "application/json"
        }
        resp = requests.post(
            "https://api.resend.com/emails",
            headers=headers,
            json=payload,
            timeout=15
        )
        if resp.status_code == 200:
            data = resp.json()
            return {"success": True, "method": "resend_api", "id": data.get("id"), "has_image": bool(image_bytes)}
        else:
            error_msg = resp.text[:200]
            logger.error("[RESEND_ERROR] API response %s: %s", resp.status_code, error_msg)
            return {"success": False, "method": "resend_api", "error": f"HTTP {resp.status_code}: {error_msg}"}
    except requests.exceptions.Timeout:
        return {"success": False, "method": "resend_api", "error": "API request timeout (15s)"}
    except requests.exceptions.ConnectionError as e:
        return {"success": False, "method": "resend_api", "error": f"Connection error: {e}"}
    except Exception as e:
        return {"success": False, "method": "resend_api", "error": str(e)}


def send_email_report(partner_email: str, assessment: Assessment, recommendations: List[str], subject: str = None) -> Dict[str, Any]:
    import traceback as tb
    if not EMAIL_ENABLED:
        print(f"[EMAIL_ERROR] ⚠️ 未配置任何郵件憑證（EMAIL_API_KEY 或 SMTP_PASSWORD），跳過郵件發送 | assessment_id={assessment.id}")
        logger.info("No email credentials configured; skip sending email for assessment %s", assessment.id)
        return {"success": False, "error": "No email credentials configured"}

    # 若未指定主旨，使用預設標題
    if subject is None:
        subject = f"【快速中醫體質檢測】新客戶檢測通知"

    html = build_email_html(assessment, recommendations)

    # 生成報告圖片
    report_image = None
    try:
        report_image = generate_report_image(assessment, recommendations)
        if report_image:
            print(f"[EMAIL_LOG] 🖼️ 報告圖片已生成 ({len(report_image)} bytes)")
    except Exception as _img_e:
        logger.warning("[EMAIL] Image generation failed (non-fatal): %s", _img_e)
        print(f"[EMAIL_LOG] ⚠️ 圖片生成失敗（非致命，繼續發送）: {_img_e}")

    print(f"[EMAIL_LOG] 📧 準備發送郵件")
    print(f"[EMAIL_LOG]    寄件者: {SENDER_EMAIL}")
    print(f"[EMAIL_LOG]    收件者: {partner_email}")
    print(f"[EMAIL_LOG]    方式: {'Cloud API' if API_EMAIL_ENABLED else 'SMTP'}")
    print(f"[EMAIL_LOG]    主旨: {subject}")
    print(f"[EMAIL_LOG]    客戶: {assessment.name} ({assessment.phone})")
    print(f"[EMAIL_LOG]    體質: {assessment.constitution} 分數: {assessment.health_score}")
    print(f"[EMAIL_LOG]    報告圖片: {'已生成' if report_image else '未生成'}")

    # ===== 優先使用雲端 Email API（HTTP 443）=====
    if API_EMAIL_ENABLED:
        max_retries = 3
        retry_delays = [2, 5, 10]
        last_error = ""

        for attempt in range(max_retries):
            try:
                print(f"[EMAIL_LOG] 🔌 透過 API 發送郵件 [attempt {attempt + 1}/{max_retries}]...")
                if EMAIL_PROVIDER == "resend":
                    result = _send_via_resend_api(html, partner_email, subject, SENDER_EMAIL, report_image)
                else:
                    result = _send_via_sendgrid_api(html, partner_email, subject, SENDER_EMAIL, report_image)

                if result.get("success"):
                    print(f"[EMAIL_LOG] ✅ API 郵件發送成功 | {SENDER_EMAIL} → {partner_email} | assessment_id={assessment.id}")
                    logger.info("Email sent via %s API for assessment %s to %s", EMAIL_PROVIDER, assessment.id, partner_email)
                    return result
                else:
                    last_error = result.get("error", "unknown error")
                    print(f"[EMAIL_ERROR] ❌ API 發送失敗: {last_error}")
                    if attempt < max_retries - 1:
                        delay = retry_delays[attempt]
                        print(f"[EMAIL_LOG] ⏳ {delay}s 後重試...")
                        import time as _time
                        _time.sleep(delay)
                        continue
            except Exception as e:
                last_error = str(e)
                print(f"[EMAIL_ERROR] ❌ API 發送異常: {e}")
                logger.exception("API email exception for assessment %s", assessment.id)
                if attempt < max_retries - 1:
                    delay = retry_delays[attempt]
                    print(f"[EMAIL_LOG] ⏳ {delay}s 後重試...")
                    import time as _time
                    _time.sleep(delay)
                    continue
            break

        # API 所有重試失敗，嘗試 SMTP fallback
        if SMTP_ENABLED:
            print(f"[EMAIL_LOG] ⚠️ API 失敗，嘗試 SMTP fallback...")
            logger.warning("API failed, falling back to SMTP for assessment %s", assessment.id)
            return _send_via_smtp(html, partner_email, subject, report_image)

        print(f"[EMAIL_ERROR] ❌ 郵件發送失敗（已嘗試 {max_retries} 次 API + SMTP fallback 不可用）| assessment_id={assessment.id}")
        return {"success": False, "error": f"All retries exhausted: {last_error}"}

    # ===== 無 API 時使用 SMTP fallback =====
    elif SMTP_ENABLED:
        return _send_via_smtp(html, partner_email, subject, report_image)

    return {"success": False, "error": "No email method available"}


def _send_via_smtp(html_content: str, to_email: str, subject: str, image_bytes: bytes = None) -> Dict[str, Any]:
    """SMTP fallback 發送郵件（端口 465 SSL，支援圖片附件）"""
    import traceback as tb
    max_retries = 2
    retry_delays = [2, 5]
    last_error = ""

    for attempt in range(max_retries):
        try:
            print(f"[EMAIL_LOG] 🔌 SMTP SSL 連線中... (attempt {attempt + 1}/{max_retries})")
            import socket as _socket
            _socket.setdefaulttimeout(10)
            with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT, timeout=10) as server:
                server.login(SMTP_USERNAME, SMTP_PASSWORD)
                msg = MIMEMultipart("mixed")
                msg["Subject"] = subject
                msg["From"] = SENDER_EMAIL
                msg["To"] = to_email
                # HTML 部分
                html_part = MIMEText(html_content, "html", "utf-8")
                msg.attach(html_part)
                # 圖片附件
                if image_bytes:
                    img_part = MIMEImage(image_bytes)
                    img_part.add_header("Content-Disposition", "attachment", filename="報告圖表.png")
                    img_part.add_header("Content-ID", "<report_image.png>")
                    msg.attach(img_part)
                server.sendmail(SENDER_EMAIL, [to_email], msg.as_string())
            print(f"[EMAIL_LOG] ✅ SMTP 郵件發送成功 | {SENDER_EMAIL} → {to_email}")
            return {"success": True, "method": "smtp_ssl", "has_image": bool(image_bytes)}
        except Exception as e:
            last_error = str(e)
            print(f"[EMAIL_ERROR] ❌ SMTP 失敗: {e}")
            if attempt < max_retries - 1:
                import time as _time
                _time.sleep(retry_delays[attempt])
            break

    return {"success": False, "method": "smtp_ssl", "error": last_error}

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
    return {"status": "ok", "time": now_hk().isoformat()}

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
        # ═══════════════════════════════════════════════════════════
        # [ATTRIBUTION] 歸因驗證與漏斗記錄
        # ═══════════════════════════════════════════════════════════
        today_str = funnel.record_visit()
        inspector_name = find_inspector_name(data.referral_code)

        print(f"\n[FUNNEL] 📥 新增訪問 | date={today_str} | referral_code={data.referral_code or 'None'} | inspector={inspector_name}")

        # 歸因驗證
        if data.referral_code:
            normalized = normalize_partner_id(data.referral_code)
            if not normalized:
                print(f"[ATTRIBUTION_ERROR] ❌ 無效的推薦碼格式: {data.referral_code}")
                logger.warning("[ATTRIBUTION] Invalid referral code format: %s", data.referral_code)
            else:
                partner = find_partner(normalized)
                if partner:
                    print(f"[ATTRIBUTION] ✅ 歸因成功 | code={data.referral_code} → name={partner.get('name', 'N/A')}")
                else:
                    print(f"[ATTRIBUTION_ERROR] ⚠️ 檢測員不在白名單 | code={data.referral_code}")
                    logger.warning("[ATTRIBUTION] Inspector not in whitelist: %s", data.referral_code)
        else:
            print(f"[ATTRIBUTION] ℹ️ 散客流量（無推薦碼）")

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

            # 記錄漏斗提交
            funnel.record_submission(data.referral_code, inspector_name)
            print(f"[FUNNEL] ✅ 問卷提交成功 | assessment_id={record.id} | 體質={analysis['constitution']}")
            
            # 準備推薦建議
            recommendations = get_recommendations(analysis["constitution"])
            symptoms = data.answers.get("symptoms", [])

            # ═══════════════════════════════════════════════════════════
            # Step A: 同步寫入 Google Sheet 客戶記錄
            # ═══════════════════════════════════════════════════════════
            try:
                write_customer_record_to_sheet(record, symptoms, 0)
                sheet_result = "success"
                funnel.record_sheet_write(True)
                print(f"[FUNNEL] ✅ Sheet 寫入成功 | assessment_id={record.id}")
            except Exception as _sheet_e:
                _sheet_tb = __import__('traceback').format_exc()
                logger.error("[SHEET] Synchronous write FAILED (non-fatal): %s\n%s", _sheet_e, _sheet_tb)
                print(f"[SHEET_ERROR] ❌ 同步寫入客戶記錄失敗（非致命，繼續執行）")
                print(f"[SHEET_ERROR]    錯誤: {_sheet_e}")
                print(f"[SHEET_ERROR]    完整 traceback:\n{_sheet_tb}")
                print(f"[FUNNEL_DROP] ❌ 漏斗流失: Sheet 寫入失敗 | assessment_id={record.id}")
                funnel.record_sheet_write(False)
                sheet_result = f"error: {_sheet_e}"

            # ═══════════════════════════════════════════════════════════
            # Step B: 同步發送 Email（確保錯誤可見）
            # ═══════════════════════════════════════════════════════════
            inspector_email = find_inspector_email(data.referral_code)
            email_target = inspector_email
            email_status = "skipped"
            email_result_detail = ""

            if SMTP_ENABLED:
                try:
                    _email_result = send_email_report(
                        inspector_email,
                        record,
                        recommendations,
                        "【COSWAY 健康檢測通知】新客戶已完成 AI 中醫體質檢測！"
                    )
                    email_status = "sent" if _email_result.get("success") else "failed"
                    email_result_detail = _email_result.get("error", "")
                    funnel.record_email_send(_email_result.get("success", False))
                    if _email_result.get("success"):
                        print(f"[FUNNEL] ✅ Email 發送成功 | {SENDER_EMAIL} → {inspector_email}")
                    else:
                        print(f"[FUNNEL_DROP] ⚠️ Email 發送失敗 | {inspector_email} | {_email_result.get('error', 'unknown')}")
                except Exception as _email_e:
                    _email_tb = __import__('traceback').format_exc()
                    logger.error("[EMAIL] Synchronous send FAILED (non-fatal): %s\n%s", _email_e, _email_tb)
                    print(f"[EMAIL_ERROR] ❌ 同步發送郵件失敗（非致命，繼續執行）")
                    print(f"[EMAIL_ERROR]    錯誤: {_email_e}")
                    print(f"[EMAIL_ERROR]    完整 traceback:\n{_email_tb}")
                    print(f"[FUNNEL_DROP] ❌ 漏斗流失: Email 發送異常 | assessment_id={record.id}")
                    funnel.record_email_send(False)
                    email_status = "exception"
                    email_result_detail = str(_email_e)
            else:
                print(f"[EMAIL_ERROR] ⚠️ SMTP 未配置（SMTP_PASSWORD 為空），跳過郵件發送 | assessment_id={record.id}")
                logger.info("SMTP not configured; skip email for assessment %s", record.id)
                email_status = "skipped (SMTP disabled)"
                funnel.record_email_send(False)
            
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
                    "constitution": get_constitution_full(analysis["constitution"]),
                    "score": analysis["score"],
                    "risk_level": analysis["risk_level"],
                    "potential_risks": potential_risks,
                    "severe_risks": severe_risks,
                    "risk_indicators": risk_indicators,
                    "risk_indicator_pcts": get_risk_indicator_pcts(analysis["score"]),
                },
                "email_sent": EMAIL_ENABLED,
                "email_target": email_target,
                "email_status": email_status,
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
                "risk_indicator_pcts": get_risk_indicator_pcts(50),
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
            constitution=get_constitution_full(record.constitution),
            health_score=record.health_score,
            risk_level=record.risk_level,
            risk_indicators=json.loads(record.risk_indicators) if record.risk_indicators else [],
            recommendations=RECOMMENDATIONS.get(record.constitution, []),
            risk_indicator_pcts=get_risk_indicator_pcts(record.health_score),
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

# ═══════════════════════════════════════════════════════════════
# [DIAGNOSTIC] 診斷與漏斗統計端點
# ═══════════════════════════════════════════════════════════════

@app.get("/api/diagnostic")
def get_diagnostic():
    """系統診斷端點：返回環境變數狀態與資料庫統計"""
    import sqlite3
    db_stats = {}
    try:
        conn = sqlite3.connect(DATABASE_URL.replace("sqlite:///", ""))
        cursor = conn.cursor()
        # 總記錄數
        cursor.execute("SELECT COUNT(*) FROM assessments")
        db_stats["total_records"] = cursor.fetchone()[0]
        # 今日記錄數（ submitted_at 已存為香港時間，改用參數比對避免時區問題）
        hk_today = now_hk().strftime("%Y-%m-%d")
        cursor.execute("SELECT COUNT(*) FROM assessments WHERE DATE(submitted_at) = ?", (hk_today,))
        db_stats["today_records"] = cursor.fetchone()[0]
        # 有檢測員 vs 散客
        cursor.execute("SELECT COUNT(*) FROM assessments WHERE referral_code IS NOT NULL AND referral_code != ''")
        db_stats["with_referral"] = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM assessments WHERE referral_code IS NULL OR referral_code = ''")
        db_stats["default_traffic"] = cursor.fetchone()[0]
        # 已聯絡比例
        cursor.execute("SELECT COUNT(*) FROM assessments WHERE contacted = 1")
        db_stats["contacted"] = cursor.fetchone()[0]
        # 按體質分組統計
        cursor.execute("SELECT constitution, COUNT(*) as cnt FROM assessments GROUP BY constitution ORDER BY cnt DESC")
        db_stats["by_constitution"] = [{"constitution": row[0], "count": row[1]} for row in cursor.fetchall()]
        conn.close()
    except Exception as e:
        db_stats["error"] = str(e)

    return {
        "status": "ok",
        "timestamp": now_hk().isoformat(),
        "database": db_stats,
        "env_vars": {
            "SMTP_ENABLED": SMTP_ENABLED,
            "API_EMAIL_ENABLED": API_EMAIL_ENABLED,
            "EMAIL_PROVIDER": EMAIL_PROVIDER,
            "SENDER_EMAIL": SENDER_EMAIL,
            "SMTP_SERVER": SMTP_SERVER,
            "SMTP_PORT": str(SMTP_PORT),
            "GOOGLE_CUSTOMER_SHEET_ID": GOOGLE_CUSTOMER_SHEET_ID,
            "has_google_creds": bool(GOOGLE_CREDENTIALS_JSON or GOOGLE_SERVICE_ACCOUNT_JSON),
            "has_email_api_key": bool(EMAIL_API_KEY),
            "has_email_password": bool(SMTP_PASSWORD),
        },
        "endpoints": {
            "health": "/api/health",
            "referral_validate": "/api/referral/validate?code=HK241545",
            "funnel_metrics": "/api/funnel/metrics",
            "attribution_report": "/api/diagnostic/attribution",
            "submit": "/api/questionnaire/submit",
            "email_api_info": "See env_vars.API_EMAIL_ENABLED / EMAIL_PROVIDER",
        }
    }

@app.get("/api/funnel/metrics")
def get_funnel_metrics(days: int = 7):
    """轉換漏斗統計端點"""
    return funnel.get_daily_stats(days)

@app.get("/api/diagnostic/attribution")
def get_attribution_report(days: int = 7):
    """歸因報告端點"""
    return funnel.get_attribution_report(days)

@app.get("/api/diagnostic/flush")
def flush_funnel_metrics():
    """清空漏斗統計（用於測試）"""
    global funnel
    funnel = FunnelMetrics()
    return {"success": True, "message": "漏斗統計已清空"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)

from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from datetime import datetime, timedelta
import json
import os
from typing import Optional, List

# =========================
# CONFIG
# =========================
CODES_FILE = "/root/bot/premium_codes.json"
USAGE_FILE = "/root/bot/pwa_usage.json"

app = FastAPI(title="Trade Prosperity API")

# CORS — allow PWA to connect
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Change to your domain later
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# =========================
# DATA MODELS
# =========================
class LoginRequest(BaseModel):
    code: str

class LoginResponse(BaseModel):
    success: bool
    message: str
    username: Optional[str] = None
    expires: Optional[str] = None

class TradeRequest(BaseModel):
    code: str
    section: str  # "nifty" or "equity"

# =========================
# HELPER FUNCTIONS
# =========================
def load_codes():
    try:
        with open(CODES_FILE, "r") as f:
            return json.load(f)
    except:
        return {}

def load_usage():
    try:
        with open(USAGE_FILE, "r") as f:
            return json.load(f)
    except:
        return {}

def save_usage(usage):
    try:
        with open(USAGE_FILE, "w") as f:
            json.dump(usage, f, indent=2)
    except Exception as e:
        print(f"Usage save error: {e}")

def verify_code(code: str):
    """Verify if code is valid and active"""
    codes = load_codes()
    if code not in codes:
        return None, "Invalid code"
    
    info = codes[code]
    if not info.get("active", True):
        return None, "Code expired or deactivated"
    
    # Check expiry
    try:
        expires = datetime.fromisoformat(info["expires"])
        if datetime.now() >= expires:
            return None, "Code has expired"
    except:
        return None, "Invalid expiry date"
    
    return info, "Valid"

def check_daily_limit(code: str, section: str, limit: int = 5):
    """Check if user has reached daily limit for a section"""
    usage = load_usage()
    today = datetime.now().strftime("%Y-%m-%d")
    key = f"{code}_{section}_{today}"
    
    current = usage.get(key, 0)
    return current < limit, current, limit - current

def increment_usage(code: str, section: str):
    """Increment usage counter"""
    usage = load_usage()
    today = datetime.now().strftime("%Y-%m-%d")
    key = f"{code}_{section}_{today}"
    
    usage[key] = usage.get(key, 0) + 1
    save_usage(usage)
    return usage[key]

# =========================
# API ENDPOINTS
# =========================
@app.get("/")
def root():
    return {"status": "online", "app": "Trade Prosperity API"}

@app.get("/health")
def health():
    return {"status": "healthy"}

@app.post("/api/login", response_model=LoginResponse)
async def login(req: LoginRequest):
    """Validate premium code"""
    info, msg = verify_code(req.code)
    
    if not info:
        raise HTTPException(status_code=401, detail=msg)
    
    return LoginResponse(
        success=True,
        message="Login successful",
        username=info.get("username", "User"),
        expires=info.get("expires")
    )

@app.get("/api/auto-signals")
async def get_auto_signals(code: str):
    """Get latest auto signals from Nifty premium channel"""
    info, msg = verify_code(code)
    if not info:
        raise HTTPException(status_code=401, detail=msg)
    
    # TODO: Fetch from Discord channel or cache
    # For now return mock data
    return {
        "signals": [
            {
                "timestamp": datetime.now().isoformat(),
                "symbol": "NIFTY",
                "action": "CALL BUY",
                "price": 23997.55,
                "confidence": 95,
                "score": "7/9",
                "entry": 23944.79,
                "target": 24100,
                "sl": 23896.79,
            }
        ],
        "count": 1,
    }

@app.post("/api/trade/nifty")
async def get_nifty_trade(req: TradeRequest):
    """Manual Nifty trade request — 5/day limit"""
    info, msg = verify_code(req.code)
    if not info:
        raise HTTPException(status_code=401, detail=msg)
    
    # Check daily limit
    can_trade, used, remaining = check_daily_limit(req.code, "nifty", 5)
    
    if not can_trade:
        raise HTTPException(
            status_code=429,
            detail=f"Daily limit reached! Aaj ke 5 Nifty trades use ho gaye. Kal subah reset hoga."
        )
    
    # Increment usage
    increment_usage(req.code, "nifty")
    
    # TODO: Trigger bot analysis and return real data
    # For now return mock
    return {
        "success": True,
        "used": used + 1,
        "remaining": remaining - 1,
        "data": {
            "symbol": "NIFTY",
            "price": 23997.55,
            "change": -0.69,
            "signal": "BULLISH — CALL BUY",
            "atm": 24000,
            "rsi": 57.8,
            "ema9": 24017.45,
            "ema14": 24016.5,
            "candle_pattern": "Hammer 🔨 (Bullish)",
            "fib_zone": "Support zone (38.2%)",
            "entry": 23944.79,
            "target": 24100,
            "sl": 23896.79,
            "confidence": 95,
            "timestamp": datetime.now().isoformat(),
        }
    }

@app.post("/api/trade/equity")
async def get_equity_trade(req: TradeRequest):
    """Manual Equity trade request — 5/day limit"""
    info, msg = verify_code(req.code)
    if not info:
        raise HTTPException(status_code=401, detail=msg)
    
    # Check daily limit
    can_trade, used, remaining = check_daily_limit(req.code, "equity", 5)
    
    if not can_trade:
        raise HTTPException(
            status_code=429,
            detail=f"Daily limit reached! Aaj ke 5 Equity analyses use ho gaye. Kal subah reset hoga."
        )
    
    # Increment usage
    increment_usage(req.code, "equity")
    
    # TODO: Trigger bot equity analysis
    # For now return mock
    return {
        "success": True,
        "used": used + 1,
        "remaining": remaining - 1,
        "sections": {
            "high_volume": [
                {
                    "symbol": "RELIANCE",
                    "price": 1436.0,
                    "change": 1.16,
                    "signal": "BUY",
                    "entry": 1436.0,
                    "target": 1480.0,
                    "sl": 1415.0,
                    "rr": "1:2.1"
                }
            ],
            "long_term": [
                {
                    "symbol": "TCS",
                    "price": 4021.0,
                    "change_3m": -3.66,
                    "signal": "NEUTRAL",
                    "entry": 4021.0,
                    "target": 4250.0,
                    "sl": 3900.0,
                }
            ],
            "selected": []
        },
        "timestamp": datetime.now().isoformat(),
    }

@app.get("/api/usage")
async def get_usage(code: str):
    """Get today's usage stats"""
    info, msg = verify_code(code)
    if not info:
        raise HTTPException(status_code=401, detail=msg)
    
    today = datetime.now().strftime("%Y-%m-%d")
    usage = load_usage()
    
    nifty_used = usage.get(f"{code}_nifty_{today}", 0)
    equity_used = usage.get(f"{code}_equity_{today}", 0)
    
    return {
        "nifty": {"used": nifty_used, "remaining": 5 - nifty_used, "limit": 5},
        "equity": {"used": equity_used, "remaining": 5 - equity_used, "limit": 5},
        "resets_at": f"{datetime.now().strftime('%Y-%m-%d')} 00:00"
    }

# =========================
# RUN
# =========================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

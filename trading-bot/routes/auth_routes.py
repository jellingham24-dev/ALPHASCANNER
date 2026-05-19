import os
import requests
from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(prefix="/auth", tags=["auth"])

WHOP_API_KEY    = os.getenv("WHOP_API_KEY", "")
WHOP_PRODUCT_ID = os.getenv("WHOP_PRODUCT_ID", "")


class ValidateBody(BaseModel):
    license_key: str


@router.post("/validate")
def validate_license(body: ValidateBody):
    if not WHOP_API_KEY:
        return {"valid": False, "error": "Auth not configured on server"}
    try:
        res = requests.post(
            f"https://api.whop.com/api/v2/licenses/{body.license_key}/validate",
            headers={
                "Authorization": f"Bearer {WHOP_API_KEY}",
                "Content-Type": "application/json",
            },
            json={"metadata": {}},
            timeout=10,
        )
        if res.status_code == 200:
            data = res.json()
            valid = data.get("valid", False)
            # Optionally restrict to a specific product
            if valid and WHOP_PRODUCT_ID:
                valid = data.get("product_id") == WHOP_PRODUCT_ID
            return {"valid": valid}
        return {"valid": False}
    except Exception as e:
        return {"valid": False, "error": str(e)}

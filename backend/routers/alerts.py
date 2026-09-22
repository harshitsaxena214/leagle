from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc, update
from core.database import get_db
from models.alert import Alert
import asyncio
import json
from core.auth import get_current_user, get_current_user_ws

router = APIRouter()

# Simple in-memory WebSocket connection manager
class ConnectionManager:
    def __init__(self):
        self.active_connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        for connection in self.active_connections:
            try:
                await connection.send_json(message)
            except Exception:
                pass

manager = ConnectionManager()

@router.get("/")
async def get_alerts(db: AsyncSession = Depends(get_db), limit: int = 50, user: dict = Depends(get_current_user)):
    result = await db.execute(
        select(Alert).order_by(desc(Alert.sent_at)).limit(limit)
    )
    alerts = result.scalars().all()
    return [
        {
            "id": str(a.id), "severity": a.severity,
            "message": a.message, "acknowledged": a.acknowledged,
            "sent_at": a.sent_at.isoformat() if a.sent_at else None,
        }
        for a in alerts
    ]

@router.patch("/{alert_id}/acknowledge")
async def acknowledge_alert(alert_id: str, db: AsyncSession = Depends(get_db), user: dict = Depends(get_current_user)):
    await db.execute(
        update(Alert).where(Alert.id == alert_id).values(acknowledged=True)
    )
    await db.commit()
    return {"status": "acknowledged"}

@router.websocket("/ws")
async def websocket_alerts(websocket: WebSocket, db: AsyncSession = Depends(get_db)):
    """Real-time alert stream via WebSocket."""
    user = await get_current_user_ws(websocket)
    if user is None:
        await websocket.close(code=1008)
        return

    await manager.connect(websocket)
    try:
        while True:
            # Poll for new unacknowledged alerts every 5 seconds
            result = await db.execute(
                select(Alert)
                .where(Alert.acknowledged == False)
                .order_by(desc(Alert.sent_at))
                .limit(5)
            )
            alerts = result.scalars().all()
            if alerts:
                await websocket.send_json({
                    "type": "alerts",
                    "data": [{"id": str(a.id), "severity": a.severity, "message": a.message} for a in alerts]
                })
            await asyncio.sleep(5)
    except WebSocketDisconnect:
        manager.disconnect(websocket)

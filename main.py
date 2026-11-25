import json
import uuid
from pathlib import Path
from fastapi import FastAPI, File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from database import (
    authenticate_user,
    create_group,
    get_all_users,
    get_group,
    get_group_messages,
    get_groups,
    get_user,
    init_db,
    save_message,
)

app = FastAPI(title="AI & DS Department Chatbox")
init_db()

BASE_DIR = Path(__file__).parent
HTML_PATH = BASE_DIR / "templates" / "chat.html"
UPLOAD_DIR = BASE_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

app.mount("/uploads", StaticFiles(directory=str(UPLOAD_DIR)), name="uploads")

ALLOWED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".pdf"}
MAX_FILE_BYTES = 15 * 1024 * 1024  # 15 MB


class GroupCreateRequest(BaseModel):
    creator_id: str
    group_name: str
    description: str = "Classroom Academic Group"
    mentor_only_post: bool = False


class ConnectionManager:
    def __init__(self):
        self.active_connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, payload: dict):
        message_text = json.dumps(payload)
        for connection in list(self.active_connections):
            try:
                await connection.send_text(message_text)
            except Exception:
                self.disconnect(connection)


manager = ConnectionManager()


def bot_reply(text: str) -> str | None:
    lowered = text.lower()
    if not lowered.startswith("@bot"):
        return None
    if "mentor" in lowered:
        return "Your class mentor is Dr. Ben M Jebin (Assistant Professor), M.Sc Artificial Intelligence and Data Science."
    if "privacy" in lowered or "number" in lowered:
        return "Zero personal phone numbers are stored or visible. All profiles use Register Numbers and Faculty IDs."
    return "DeptBot active: Type '@bot mentor' or '@bot privacy', or ask an academic question."


@app.get("/", response_class=HTMLResponse)
async def home():
    return HTML_PATH.read_text(encoding="utf-8")


@app.get("/api/members")
async def list_members():
    return {"members": get_all_users()}


@app.get("/api/groups/{group_id}/messages")
async def group_history(group_id: int):
    grp = get_group(group_id)
    if not grp:
        raise HTTPException(status_code=404, detail="Group not found")
    return {"group": grp, "messages": get_group_messages(group_id)}


@app.post("/api/groups")
async def add_classroom_group(req: GroupCreateRequest):
    user = get_user(req.creator_id)
    if not user or user["role"] != "MENTOR":
        raise HTTPException(status_code=403, detail="Only the Mentor can create classroom groups.")
    if not req.group_name.strip():
        raise HTTPException(status_code=400, detail="Group name is required.")

    new_group = create_group(
        group_name=req.group_name,
        description=req.description or "Classroom Group",
        mentor_only_post=req.mentor_only_post,
        created_by=user["user_id"],
    )
    await manager.broadcast({"type": "new_group", "group": new_group})
    return {"group": new_group}


@app.post("/api/upload")
async def upload_faculty_file(
    sender_id: str = Form(...),
    group_id: int = Form(...),
    caption: str = Form(""),
    file: UploadFile = File(...),
):
    user = get_user(sender_id)
    if not user or user["role"] != "MENTOR":
        raise HTTPException(status_code=403, detail="Only faculty/mentors can upload pictures and PDFs.")

    grp = get_group(group_id)
    if not grp:
        raise HTTPException(status_code=404, detail="Classroom group not found.")

    orig_name = Path(file.filename or "attachment").name
    ext = Path(orig_name).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail="Only JPG, PNG, WEBP images and PDF files are allowed.")

    if file.size is not None and file.size > MAX_FILE_BYTES:
        raise HTTPException(status_code=400, detail="File size exceeds 15 MB limit.")

    content_bytes = await file.read()
    if len(content_bytes) > MAX_FILE_BYTES:
        raise HTTPException(status_code=400, detail="File size exceeds 15 MB limit.")

    stored_name = f"{uuid.uuid4().hex}{ext}"
    dest_path = UPLOAD_DIR / stored_name
    dest_path.write_bytes(content_bytes)

    file_url = f"/uploads/{stored_name}"
    message_text = caption.strip() or f"Shared a file: {orig_name}"

    saved = save_message(
        group_id=group_id,
        sender_id=user["user_id"],
        sender_name=user["full_name"],
        sender_role=user["role"],
        content=message_text,
        attachment_url=file_url,
        attachment_name=orig_name,
    )
    await manager.broadcast({"type": "message", "data": saved})
    return {"status": "ok", "message": saved}


@app.websocket("/ws/{user_id}")
async def websocket_endpoint(websocket: WebSocket, user_id: str, secret: str = ""):
    user = authenticate_user(user_id, secret)
    if not user:
        await websocket.accept()
        await websocket.send_text(
            json.dumps({
                "type": "error",
                "message": "Invalid ID, birth date password, or 7-digit mentor code.",
            })
        )
        await websocket.close(code=1008)
        return

    await manager.connect(websocket)

    groups = get_groups()
    first_group_id = groups[0]["group_id"] if groups else 1

    await websocket.send_text(
        json.dumps({
            "type": "init",
            "user": user,
            "groups": groups,
            "members": get_all_users(),
            "active_group_id": first_group_id,
            "messages": get_group_messages(first_group_id),
        })
    )

    try:
        while True:
            raw_data = await websocket.receive_text()
            try:
                data = json.loads(raw_data)
                group_id = int(data.get("group_id", first_group_id))
            except (json.JSONDecodeError, ValueError, TypeError):
                await websocket.send_text(
                    json.dumps({"type": "error", "message": "Invalid message payload."})
                )
                continue

            content = (data.get("content") or "").strip()
            if not content:
                continue

            grp = get_group(group_id)
            if not grp:
                continue

            if grp["mentor_only_post"] == 1 and user["role"] != "MENTOR":
                await websocket.send_text(
                    json.dumps({"type": "notice", "message": "Only Dr. Ben M Jebin can post in this announcement group."})
                )
                continue

            saved = save_message(
                group_id=group_id,
                sender_id=user["user_id"],
                sender_name=user["full_name"],
                sender_role=user["role"],
                content=content,
            )
            await manager.broadcast({"type": "message", "data": saved})

            reply = bot_reply(content)
            if reply:
                bot_msg = save_message(
                    group_id=group_id,
                    sender_id="DEPT_BOT",
                    sender_name="DeptBot",
                    sender_role="BOT",
                    content=reply,
                )
                await manager.broadcast({"type": "message", "data": bot_msg})

    except WebSocketDisconnect:
        manager.disconnect(websocket)
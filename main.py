from typing import Annotated, Optional

from fastapi import Depends, FastAPI, HTTPException, Query,  WebSocket, WebSocketDisconnect, Response
from fastapi.responses import StreamingResponse
from sqlmodel import Field, Session, SQLModel, create_engine, select
from pydantic import BaseModel
from fastapi.middleware.cors import CORSMiddleware
import json
import asyncio


class UserResponse(BaseModel):
    id: int
    code: str
    initial: Optional[bool]
    final: Optional[bool]

    class Config:
        orm_mode = True

class CommentUpdate(BaseModel):
    id: int
    status: int  # 0, 1, or 2

class CommentCreate(BaseModel):
    user_code: str
    comment: str

class User(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    code: str = Field(index=True)
    initial: Optional[bool] = Field(default=None)
    final: Optional[bool] = Field(default=None)

class Comment(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    user_code: str = Field(index=True)
    comment: str
    status: int = Field(default=0)  # 0=未审核, 1=通过, 2=拒绝


sqlite_file_name = "database.db"
sqlite_url = f"sqlite:///{sqlite_file_name}"

connect_args = {"check_same_thread": False}
engine = create_engine(sqlite_url, connect_args=connect_args)


def create_db_and_tables():
    SQLModel.metadata.create_all(engine)


def get_session():
    with Session(engine) as session:
        yield session


SessionDep = Annotated[Session, Depends(get_session)]


app = FastAPI()

# 存储所有 SSE 连接的广播器
class CommentBroadcaster:
    def __init__(self):
        self.connections = set()
        self.lock = asyncio.Lock()

    async def subscribe(self):
        """客户端订阅 SSE 连接"""
        queue = asyncio.Queue()
        async with self.lock:
            self.connections.add(queue)
        try:
            while True:
                data = await queue.get()
                yield f"data: {json.dumps(data)}\n\n"
        finally:
            async with self.lock:
                self.connections.remove(queue)

    async def broadcast(self, data):
        """广播数据给所有客户端"""
        async with self.lock:
            for queue in self.connections:
                await queue.put(data)

broadcaster = CommentBroadcaster()

# 获取所有已通过（status=1）的评论
def get_approved_comments(session: Session):
    return session.exec(select(Comment).where(Comment.status == 1)).all()

# 数据库依赖
def get_session():
    with Session(engine) as session:
        yield session

SessionDep = Annotated[Session, Depends(get_session)]

# CORS中间件配置
origins = [
    "http://localhost:5173",
    "https://your-frontend-domain.com",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,            # 允许哪些域访问
    allow_credentials=True,           # 是否允许发送 cookies 等凭证
    allow_methods=["*"],              # 允许的请求方法，"*" 表示所有
    allow_headers=["*"],              # 允许的请求头，"*" 表示所有
)

# 初始化数据库
@app.on_event("startup")
def on_startup():
    SQLModel.metadata.create_all(engine)

@app.on_event("startup")
def on_startup():
    create_db_and_tables()


@app.post("/users/login/{code}", response_model=UserResponse)
def check_initial_status(code: str, session: SessionDep):
    user = session.exec(select(User).where(User.code == code)).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    return user
    

@app.post("/users/update/{user_id}/{new_status}")
def update_status(
    user_id: int, 
    new_status: bool, 
    session: SessionDep
) -> dict[str, str]:
    user = session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="user not found")

    if user.initial is None:
        user.initial = new_status
    user.final = new_status
    
    session.add(user)
    session.commit()
    session.refresh(user)
    
    return {"message": "user status updated successfully"}

# 接口1: 创建新评论
@app.post("/comments/")
def create_comment(comment_data: CommentCreate, session: SessionDep):
    new_comment = Comment(
        user_code=comment_data.user_code,
        comment=comment_data.comment,
        status=0
    )
    session.add(new_comment)
    session.commit()
    session.refresh(new_comment)
    
    # 通知WebSocket客户端
    manager.broadcast(json.dumps({"event": "new_comment", "data": new_comment.dict()}))
    
    return {"message": "Comment created", "id": new_comment.id}

# 接口2: 获取第一个待审核评论
@app.get("/comments/pending")
def get_first_pending_comment(session: SessionDep):
    comment = session.exec(
        select(Comment)
        .where(Comment.status == 0)
        .order_by(Comment.id)
        ).first()
    if not comment:
        raise HTTPException(status_code=404, detail="No pending comments")
    return comment

# 接口3: SSE获取所有已通过评论
@app.get("/comments/sse_approved")
async def sse_approved_comments(session: Session = Depends(get_session)):
    async def event_generator():
        # 首次连接时发送当前所有已通过的评论
        approved_comments = get_approved_comments(session)
        initial_data = {
            "event": "initial_data",
            "data": [comment.dict() for comment in approved_comments]
        }
        yield f"data: {json.dumps(initial_data)}\n\n"

        # 订阅后续更新
        async for update in broadcaster.subscribe():
            yield update

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
    )

# 接口4: 更新评论状态
@app.post("/comments/update_status")
async def update_comment_status(update: CommentUpdate, session: SessionDep):
    comment = session.get(Comment, update.id)
    if not comment:
        raise HTTPException(status_code=404, detail="Comment not found")
    
    if update.status not in {0, 1, 2}:
        raise HTTPException(status_code=400, detail="Invalid status code")
    
    comment.status = update.status
    session.add(comment)
    session.commit()
    session.refresh(comment)
    
   # 如果状态更新为 1，广播最新数据
    if comment.status == 1:
        approved_comments = get_approved_comments(session)
        update_data = {
            "event": "status_updated",
            "data": [c.dict() for c in approved_comments]
        }
        await broadcaster.broadcast(update_data)

    return {"message": "Status updated"}
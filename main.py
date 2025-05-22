from typing import Annotated, Optional

from fastapi import Depends, FastAPI, HTTPException, Query
from sqlmodel import Field, Session, SQLModel, create_engine, select
from pydantic import BaseModel

class UserResponse(BaseModel):
    id: int
    code: str
    initial: Optional[bool]
    final: Optional[bool]


class User(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    code: str = Field(index=True)
    initial: Optional[bool] = Field(default=None)
    final: Optional[bool] = Field(default=None)


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
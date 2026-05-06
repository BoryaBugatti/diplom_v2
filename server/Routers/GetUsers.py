from fastapi import APIRouter, HTTPException, Depends
from sqlalchemy.orm import Session
from config.dbconfig import SessionLocal
from crud.user_crud import get_all_users
def get_db():
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except:
        db.rollback()
        raise
    finally:
        db.close()

router = APIRouter(prefix="/users")

@router.get("")
def GetUsers(db:Session=Depends(get_db)):
    return {"users": get_all_users(db)}